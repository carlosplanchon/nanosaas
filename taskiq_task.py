#!/usr/bin/env python3

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from taskiq import TaskiqEvents, TaskiqMiddleware, TaskiqResult, TaskiqState
from taskiq.exceptions import NoResultError
from taskiq.receiver import Receiver as BaseReceiver
from taskiq.serializers import JSONSerializer
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend
from shared import CONFIG

from tortoise import Tortoise
from web.db_models import TaskError, TaskHistory
from shared import TORTOISE_ORM

import asyncio
import logging
import time


# Seconds a task can wait in the queue, and run, before it's given up.
QUEUE_TIMEOUT = CONFIG["taskiq"].get("queue_timeout", 600)
TIME_LIMIT = CONFIG["taskiq"].get("time_limit", 600)

# Retry with new connections for about a second, which covers a Redis restart, and fail soon after.
# A retried LPUSH queues a task twice if Redis ran the first one and only its reply was lost:
# the Receiver runs one copy.
REDIS_OPTIONS = {
    "retry": Retry(ExponentialBackoff(cap=0.8, base=0.2), 2),
    "retry_on_error": [RedisConnectionError, RedisTimeoutError],
    "socket_connect_timeout": 2,
}


class TaskExpired(Exception):
    """A task that waited in the queue longer than queue_timeout: it was dropped without running."""


class SaveFinishedTasks(TaskiqMiddleware):
    """
    Save every task in its history row as soon as it ends, instead of
    waiting for someone to check it before the result backend forgets
    it (after a day). Failed tasks get their credit back.
    """

    async def post_execute(self, message, result):
        if isinstance(result.error, NoResultError):
            # A copy the Receiver skipped: it didn't run.
            return
        try:
            await TaskHistory.finish(message.task_id, result.return_value, result.error)
        except Exception as e:
            # The result still reaches the result backend, and the API saves it from there.
            print(f"Error saving task {message.task_id}: {e}")


class Broker(ListQueueBroker):
    """
    Waits for tasks a second at a time, so a worker told to stop notices
    it, and keeps waiting when Redis restarts.
    """

    async def listen(self):
        while True:
            try:
                async with Redis(connection_pool=self.connection_pool) as redis:
                    popped = await redis.brpop([self.queue_name], timeout=1)
            except RedisError as e:
                print(f"Can't get tasks from Redis, trying again in a second: {e}")
                await asyncio.sleep(1)
                continue
            if popped:
                yield popped[1]


broker = Broker(CONFIG["taskiq"]["broker"], **REDIS_OPTIONS).with_result_backend(
    RedisAsyncResultBackend(
        CONFIG["taskiq"]["backend"],
        result_ex_time=24 * 60 * 60,
        serializer=JSONSerializer(),
        prefix_str="nanosaas-result",
        **REDIS_OPTIONS,
    )
).with_middlewares(SaveFinishedTasks())


class Receiver(BaseReceiver):
    """
    The worker's receiver: it runs only the first copy of a pending task,
    and drops the tasks that waited in the queue too long.
    """

    @staticmethod
    def skipped(message) -> TaskiqResult:
        """
        The result of a task that didn't run. NoResultError keeps it out of
        the result backend, where it would replace the result of the copy
        that ran, and SaveFinishedTasks keeps it out of the history.
        """
        return TaskiqResult(
            is_err=True,
            return_value=None,
            execution_time=0,
            labels=message.labels,
            error=NoResultError(),
        )

    async def run_task(self, target, message, ack_controller=None):
        # Before the queue_timeout check, so a late copy of a task that
        # already started can't fail it as expired.
        try:
            started = await TaskHistory.start(message.task_id)
        except Exception as e:
            # Another copy may be running. If none is, it fails at its deadline.
            print(f"Can't start task {message.task_id}, so it's skipped: {e}")
            return self.skipped(message)
        if not started:
            print(f"Task {message.task_id} was started already, or given up by the API: skipped.")
            return self.skipped(message)

        if time.time() > float(message.labels.get("queue_deadline", "inf")):
            print(f"Task {message.task_id} waited in the queue longer than queue_timeout: dropped.")
            return TaskiqResult(
                is_err=True,
                return_value=None,
                execution_time=0,
                labels=message.labels,
                error=TaskExpired(),
            )

        result = await super().run_task(target, message, ack_controller)
        if isinstance(result.error, TaskError):
            # Instead of the traceback that HideTaskErrors keeps out.
            print(f"Task {message.task_id} failed with a TaskError: {result.error}")
        return result


class HideTaskErrors(logging.Filter):
    """
    Keeps out of the log TaskIQ's record of a task that raised a TaskError,
    with its traceback: the task failed on purpose, with a message for the
    user, and the Receiver logs it in a line.
    """

    def filter(self, record):
        return not (record.exc_info and isinstance(record.exc_info[1], TaskError))


# The logger of TaskIQ's receiver, which logs the tasks' exceptions.
logging.getLogger("taskiq.receiver.receiver").addFilter(HideTaskErrors())


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def startup(state: TaskiqState):
    # taskiq 0.13.0 doesn't set up logging in forkserver workers, the default since Python 3.14.
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s][%(name)s][%(levelname)-7s][%(processName)s] %(message)s",
    )
    state.tortoise = await Tortoise.init(config=TORTOISE_ORM)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown(state: TaskiqState):
    await state.tortoise.close_connections()


@broker.task(task_name="divide")
async def divide(x: int, y: int, user_id: str):
    """Divide x by y. It waits 5 seconds first, like a long-running task."""
    await asyncio.sleep(5)  # Simulate a long-running operation
    if y == 0:
        # It fails, so its credit comes back, and the user sees this message.
        raise TaskError("Can't divide by zero.")
    return x / y
