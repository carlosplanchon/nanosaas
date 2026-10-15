#!/usr/bin/env python3

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from taskiq import TaskiqEvents, TaskiqMiddleware, TaskiqResult, TaskiqState
from taskiq.receiver import Receiver as BaseReceiver
from taskiq.serializers import JSONSerializer
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend
from shared import CONFIG

from tortoise import Tortoise
from web.db_models import Account, TaskHistory
from shared import TORTOISE_ORM

import asyncio
import logging
import time


# Seconds a task can wait in the queue, and run, before it's given up.
QUEUE_TIMEOUT = CONFIG["taskiq"].get("queue_timeout", 600)
TIME_LIMIT = CONFIG["taskiq"].get("time_limit", 600)

# Retry with new connections for about a second, which covers a Redis restart, and fail soon after.
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
    """The worker's receiver: it drops the tasks that waited in the queue too long."""

    async def run_task(self, target, message, ack_controller=None):
        if time.time() > float(message.labels.get("queue_deadline", "inf")):
            print(f"Task {message.task_id} waited in the queue longer than queue_timeout: dropped.")
            return TaskiqResult(
                is_err=True,
                return_value=None,
                execution_time=0,
                labels=message.labels,
                error=TaskExpired(),
            )
        return await super().run_task(target, message, ack_controller)


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
    try:
        await asyncio.sleep(5)  # Simulate a long-running operation
        result: float = x / y
    except Exception as e:
        # The credit was reserved when the task was queued, and only
        # successful runs are charged: give it back.
        await Account.refund_credit(user_id)
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e),
        }

    return result
