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
from taskiq_redis.exceptions import ResultIsMissingError
from shared import CONFIG

from tortoise import Tortoise, timezone
from web.db_models import TaskError, TaskHistory
from shared import TORTOISE_ORM

import asyncio
import datetime
import logging
import time


# Seconds a task can wait in the queue, and run, before it's given up.
QUEUE_TIMEOUT = CONFIG["taskiq"].get("queue_timeout", 600)
TIME_LIMIT = CONFIG["taskiq"].get("time_limit", 600)

# The worker running a task holds a lease on it for LEASE seconds, and renews it every LEASE / 3.
# A task whose lease expired lost its worker: it's queued again, until it ran ATTEMPTS times.
LEASE = CONFIG["taskiq"].get("lease", 30)
ATTEMPTS = CONFIG["taskiq"].get("attempts", 3)

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


class TaskLost(Exception):
    """A task that didn't end: its worker died in each of its runs, or the broker lost it."""


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


async def queue_task(task_name: str, task_id: str, parameters: dict, user_id: str) -> None:
    """Queue a run of a task, with its parameters, the user's ID, a time limit and a deadline in the queue."""
    await broker.find_task(task_name).kicker().with_task_id(task_id).with_labels(
        timeout=TIME_LIMIT,
        queue_deadline=time.time() + QUEUE_TIMEOUT
    ).kiq(**parameters, user_id=user_id)


async def renew_lease(task_id: str, started_at: datetime.datetime) -> None:
    """Renew the lease of a task's run every LEASE / 3 seconds, until the run ends or is released."""
    while True:
        await asyncio.sleep(LEASE / 3)
        try:
            if not await TaskHistory.renew(task_id, started_at, datetime.timedelta(seconds=LEASE)):
                return
        except Exception as e:
            # The next one may get through: the lease lasts LEASE seconds.
            print(f"Can't renew the lease of task {task_id}: {e}")


async def requeue_lost_tasks() -> None:
    """
    Every LEASE / 3 seconds, queue again the tasks whose lease expired,
    because their worker died. Every worker does it, and release() lets
    only one of them take each task.
    """
    while True:
        await asyncio.sleep(LEASE / 3)
        try:
            lost = await TaskHistory.filter(
                status="PENDING", started_at__isnull=False, lease_until__lt=timezone.now()
            ).prefetch_related("user")
        except Exception as e:
            print(f"Can't look for the tasks that lost their worker: {e}")
            continue
        for task in lost:
            try:
                await requeue(task)
            except Exception as e:
                # If it was released, it fails at the API's deadline, as TaskLost.
                print(f"Can't queue task {task.task_id} again: {e}")


async def requeue(task: TaskHistory) -> None:
    """
    Queue again a task that lost its worker, or fail it as TaskLost once
    it ran ATTEMPTS times. A task that ended, but whose worker couldn't
    save it, is saved from the result backend instead.
    """
    if broker.find_task(task.task_type) is None:
        # A worker with other tasks, like one from before a deploy: one that has it queues it.
        return
    try:
        result = await broker.result_backend.get_result(task.task_id)
    except ResultIsMissingError:
        pass
    else:
        await TaskHistory.finish(task.task_id, result.return_value, result.error)
        return
    if task.attempts >= ATTEMPTS:
        if await TaskHistory.finish(task.task_id, error=TaskLost()):
            print(f"Task {task.task_id} lost its worker in each of its {task.attempts} runs: failed.")
    elif await TaskHistory.release(task.task_id, task.started_at):
        await queue_task(task.task_type, task.task_id, task.parameters, task.user.google_id)
        print(f"Task {task.task_id} lost its worker: queued again, after {task.attempts} of {ATTEMPTS} runs.")


class Receiver(BaseReceiver):
    """
    The worker's receiver: it runs only one copy of a pending task at a
    time, renewing its lease while it runs, and drops the tasks that
    waited in the queue too long.
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
            started_at = await TaskHistory.start(message.task_id, datetime.timedelta(seconds=LEASE))
        except Exception as e:
            # Another copy may be running. If none is, it fails at its deadline.
            print(f"Can't start task {message.task_id}, so it's skipped: {e}")
            return self.skipped(message)
        if started_at is None:
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

        renewing = asyncio.create_task(renew_lease(message.task_id, started_at))
        try:
            result = await super().run_task(target, message, ack_controller)
        finally:
            renewing.cancel()
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
    state.requeuing = asyncio.create_task(requeue_lost_tasks())


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown(state: TaskiqState):
    state.requeuing.cancel()
    await state.tortoise.close_connections()


@broker.task(task_name="divide")
async def divide(x: int, y: int, user_id: str):
    """Divide x by y. It waits 5 seconds first, like a long-running task."""
    await asyncio.sleep(5)  # Simulate a long-running operation
    if y == 0:
        # It fails, so its credit comes back, and the user sees this message.
        raise TaskError("Can't divide by zero.")
    return x / y
