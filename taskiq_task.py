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
from webhooks import deliver_webhooks

import asyncio
import datetime
import inspect
import logging
import multiprocessing
import tempfile
import time
from pathlib import Path


# TaskIQ's, which the API's log lines use too: with their time, logger and level.
LOG_FORMAT = "[%(asctime)s][%(name)s][%(levelname)-7s][%(processName)s] %(message)s"
logger = logging.getLogger(__name__)

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
    """
    A task that didn't end: its worker died, or stopped before it ended, in
    each of its runs, or the broker lost it.
    """


class SaveFinishedTasks(TaskiqMiddleware):
    """
    Save every task in its history row as soon as it ends, instead of
    waiting for someone to check it before the result backend forgets
    it (after a day). Failed tasks get their credits back.
    """

    async def post_execute(self, message, result):
        if isinstance(result.error, NoResultError):
            # A copy the Receiver skipped: it didn't run.
            return
        try:
            await TaskHistory.finish(message.task_id, result.return_value, result.error)
        except Exception as e:
            # The result still reaches the result backend, and the API saves it from there.
            logger.error("Error saving task %s: %s", message.task_id, e)


class Broker(ListQueueBroker):
    """
    Waits for tasks a second at a time, so a worker told to stop notices
    it, and keeps waiting when Redis restarts.
    """

    async def pop(self) -> bytes | None:
        """The next task in the queue, or None after a second without one."""
        try:
            async with Redis(connection_pool=self.connection_pool) as redis:
                popped = await redis.brpop([self.queue_name], timeout=1)
        except RedisError as e:
            logger.warning("Can't get tasks from Redis, trying again in a second: %s", e)
            await asyncio.sleep(1)
            return None
        return popped[1] if popped else None

    async def listen(self):
        while True:
            # A worker told to stop cancels this. Cancelled, the BRPOP would lose
            # a task that Redis took from the queue for it, whose reply is on its
            # way: instead, this waits for the reply, and hands that task over to
            # TaskIQ, which runs it before the worker stops.
            popping = asyncio.create_task(self.pop())
            try:
                message = await asyncio.shield(popping)
            except asyncio.CancelledError:
                # Within the BRPOP's second, or a few more: a Redis that hangs doesn't answer.
                try:
                    message = await asyncio.wait_for(popping, 5)
                except TimeoutError:
                    message = None
                if message is None:
                    raise
            if message is not None:
                yield message


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
            logger.warning("Can't renew the lease of task %s: %s", task_id, e)


async def ping_postgres() -> None:
    await Tortoise.get_connection("default").execute_query("SELECT 1")


async def ping_redis() -> None:
    """The broker's Redis, which queues the tasks, and the result backend's, if it's another."""
    for pool in (broker.connection_pool, broker.result_backend.redis_pool):
        async with Redis(connection_pool=pool) as redis:
            await redis.ping()


# Every HEARTBEAT seconds, each worker process writes the time to its heartbeat
# file, in the temporary directory, while it can use PostgreSQL and Redis:
# scripts/worker_healthcheck.py, the worker's healthcheck, reads them.
HEARTBEAT = 5


def heartbeat_file() -> Path:
    """This process's, named after it, like nanosaas-heartbeat-worker-0."""
    return Path(tempfile.gettempdir()) / f"nanosaas-heartbeat-{multiprocessing.current_process().name}"


async def beat() -> None:
    """
    Write this process's heartbeat every HEARTBEAT seconds, after PostgreSQL
    and Redis answer, within 2 seconds each. It runs in the event loop of
    the tasks, so a task with blocking code stops it too.
    """
    path = heartbeat_file()
    beating = True
    while True:
        try:
            await asyncio.wait_for(asyncio.gather(ping_postgres(), ping_redis()), 2)
            path.write_text(f"{time.time()}\n")
            if not beating:
                logger.info("Heartbeat: PostgreSQL and Redis answer again.")
            beating = True
        except Exception as e:
            # Once, not every few seconds.
            if beating:
                logger.warning("Heartbeat stopped: PostgreSQL or Redis doesn't answer: %r", e)
            beating = False
        await asyncio.sleep(HEARTBEAT)


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
            logger.error("Can't look for the tasks that lost their worker: %s", e)
            continue
        for task in lost:
            try:
                await requeue(task)
            except Exception as e:
                # If it was released, it fails at the API's deadline, as TaskLost.
                logger.error("Can't queue task %s again: %s", task.task_id, e)


async def requeue(task: TaskHistory, stopped: bool = False) -> None:
    """
    Queue again a task that lost its worker, or, with stopped, whose worker
    stopped it, or fail it as TaskLost once it ran ATTEMPTS times. A task
    that ended, but whose worker couldn't save it, is saved from the result
    backend instead.
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
            if stopped:
                logger.error(
                    "Task %s didn't end before its worker stopped, in the last of its %d runs: failed.",
                    task.task_id, task.attempts,
                )
            else:
                logger.error("Task %s lost its worker in each of its %d runs: failed.", task.task_id, task.attempts)
    elif await TaskHistory.release(task.task_id, task.started_at, expired=not stopped):
        await queue_task(task.task_type, task.task_id, task.parameters, task.user.google_id)
        why = "didn't end before its worker stopped" if stopped else "lost its worker"
        logger.warning("Task %s %s: queued again, after %d of %d runs.", task.task_id, why, task.attempts, ATTEMPTS)


async def requeue_stopped(task_id: str, started_at: datetime.datetime) -> None:
    """requeue() a task whose worker stopped its run that started at started_at."""
    task = await TaskHistory.get_or_none(
        task_id=task_id, status="PENDING", started_at=started_at
    ).prefetch_related("user")
    if task is not None:
        await requeue(task, stopped=True)


# Seconds a stopping worker gives the tasks it stops to be queued again (see Receiver.listen()).
STOP_TIMEOUT = 10


class Receiver(BaseReceiver):
    """
    The worker's receiver: it runs only one copy of a pending task at a
    time, renewing its lease while it runs, drops the tasks that waited in
    the queue too long, and, when the worker stops, queues again the ones
    that didn't end in time.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The asyncio task running each task, by the task's ID, and the IDs of the ones it stopped.
        self.running: dict[str, asyncio.Task] = {}
        self.stopped: set[str] = set()

    async def listen(self, finish_event):
        """
        Run tasks until the worker is told to stop. Then TaskIQ takes no more,
        and waits for the running ones to end, for up to --wait-tasks-timeout
        seconds: this stops the others, which run_task() queues again, within
        STOP_TIMEOUT seconds. One that doesn't stop by then is queued again
        when its lease expires, as if its worker died.
        """
        await super().listen(finish_event)
        if self.running:
            logger.warning("Stopping the %d tasks that didn't end in time, to queue them again.", len(self.running))
            self.stopped.update(self.running)
            stopping = list(self.running.values())
            for task in stopping:
                task.cancel()
            await asyncio.wait(stopping, timeout=STOP_TIMEOUT)

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
            logger.error("Can't start task %s, so it's skipped: %s", message.task_id, e)
            return self.skipped(message)
        if started_at is None:
            logger.info(
                "Task %s was started already, given up by the API, canceled, or deleted with its account: skipped.",
                message.task_id,
            )
            return self.skipped(message)

        if time.time() > float(message.labels.get("queue_deadline", "inf")):
            logger.warning("Task %s waited in the queue longer than queue_timeout: dropped.", message.task_id)
            return TaskiqResult(
                is_err=True,
                return_value=None,
                execution_time=0,
                labels=message.labels,
                error=TaskExpired(),
            )

        # The task's ID, for a task that asks for it: the same in each of its runs,
        # so it can pass it as the idempotency key of the services it calls.
        if "task_id" in inspect.signature(target).parameters:
            message.kwargs["task_id"] = message.task_id

        renewing = asyncio.create_task(renew_lease(message.task_id, started_at))
        self.running[message.task_id] = asyncio.current_task()
        try:
            result = await super().run_task(target, message, ack_controller)
        finally:
            renewing.cancel()
            del self.running[message.task_id]
        if message.task_id in self.stopped:
            # listen() cancelled it, and TaskIQ caught the CancelledError: handled here,
            # so the cancellation doesn't linger for what this asyncio task does next.
            asyncio.current_task().uncancel()
            if isinstance(result.error, asyncio.CancelledError):
                try:
                    await requeue_stopped(message.task_id, started_at)
                except Exception as e:
                    # Its lease expires, and then a worker queues it again.
                    logger.error("Can't queue task %s again: %s", message.task_id, e)
                return self.skipped(message)
        if isinstance(result.error, TaskError):
            # Instead of the traceback that HideTaskErrors keeps out.
            logger.info("Task %s failed with a TaskError: %s", message.task_id, result.error)
        return result


class HideTaskErrors(logging.Filter):
    """
    Keeps out of the log TaskIQ's record of a task that raised a TaskError,
    with its traceback: the task failed on purpose, with a message for the
    user, and the Receiver logs it in a line. Also the one of a task that the
    Receiver stopped, with a CancelledError, as the worker stopped: it logs
    that it queued it again.
    """

    def filter(self, record):
        return not (record.exc_info and isinstance(record.exc_info[1], (TaskError, asyncio.CancelledError)))


# The logger of TaskIQ's receiver, which logs the tasks' exceptions.
logging.getLogger("taskiq.receiver.receiver").addFilter(HideTaskErrors())


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def startup(state: TaskiqState):
    # taskiq 0.13.0 doesn't set up logging in forkserver workers, the default since Python 3.14.
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    state.tortoise = await Tortoise.init(config=TORTOISE_ORM)
    state.requeuing = asyncio.create_task(requeue_lost_tasks())
    state.beating = asyncio.create_task(beat())
    state.delivering = asyncio.create_task(deliver_webhooks())


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown(state: TaskiqState):
    state.requeuing.cancel()
    state.beating.cancel()
    # Its attempts stop with it, before their connections close.
    state.delivering.cancel()
    await asyncio.wait({state.delivering}, timeout=2)
    await state.tortoise.close_connections()


@broker.task(task_name="divide")
async def divide(x: int, y: int, user_id: str):
    """Divide x by y. It waits 5 seconds first, like a long-running task."""
    await asyncio.sleep(5)  # Simulate a long-running operation
    if y == 0:
        # It fails, so its credit comes back, and the user sees this message.
        raise TaskError("Can't divide by zero.")
    return x / y
