"""
A worker told to stop, as Docker does in a deploy: it takes no more tasks,
lets the ones it's running end, for up to --wait-tasks-timeout seconds, and
stops the others, queueing them again right away. A task that Redis gives
it as it stops isn't lost either.
"""
import asyncio
import os
import re
import signal
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis
from taskiq.serializers import JSONSerializer
from taskiq_redis import RedisAsyncResultBackend

from tests.helpers import Proxy, attempts, start_divide, task_row, wait_for_log, wait_for_row

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
ERROR = {"status": "error", "message": "The task failed"}


def stop(worker):
    """As Docker does: SIGTERM to the worker's main process, which tells its processes to stop."""
    os.kill(worker.proc.pid, signal.SIGTERM)


async def stopped(worker, timeout=30):
    """Wait for the worker to end, its processes and all."""
    await asyncio.to_thread(worker.proc.wait, timeout)


async def run(db, task_id):
    """A task's status, when its current run started, and how many runs it had."""
    row = await db.fetchrow("SELECT status, started_at, attempts FROM taskhistory WHERE task_id = $1", task_id)
    return row["status"], row["started_at"], row["attempts"]


async def has_result(server, task_id):
    """Whether the result backend has a result of the task."""
    backend = tomllib.loads(server.config.read_text())["taskiq"]["backend"]
    results = RedisAsyncResultBackend(backend, serializer=JSONSerializer(), prefix_str="nanosaas-result")
    try:
        return await results.is_result_ready(task_id)
    finally:
        await results.shutdown()


async def test_a_stopping_worker_lets_its_tasks_end(retry_server, start_worker, retry_db, retry_account):
    worker = start_worker(retry_server, "--wait-tasks-timeout", "60")
    task_id = await start_divide(retry_server, retry_account)
    await wait_for_log(worker.log, f"Executing task divide with ID: {task_id}")
    stop(worker)

    # It takes no more tasks: this one waits for the next worker.
    await wait_for_log(worker.log, "Waiting for 1 running tasks to complete...")
    next_id = await start_divide(retry_server, retry_account)

    # The one it was running ends, in its first run, and then the worker does.
    assert await wait_for_row(retry_db, task_id, "SUCCESS") == ("SUCCESS", 2.0)
    await stopped(worker)
    assert await attempts(retry_db, task_id) == 1
    assert "queued again" not in worker.log.read_text()
    assert await run(retry_db, next_id) == ("PENDING", None, 0)

    start_worker(retry_server)
    assert await wait_for_row(retry_db, next_id, "SUCCESS") == ("SUCCESS", 2.0)
    assert await retry_account.credits() == 3


async def test_a_task_that_doesnt_end_in_time_is_queued_again_right_away(
    retry_server, start_worker, retry_db, retry_account
):
    worker = start_worker(retry_server, "--wait-tasks-timeout", "1")
    task_id = await start_divide(retry_server, retry_account)
    await wait_for_log(worker.log, f"Executing task divide with ID: {task_id}")
    stop(worker)
    await stopped(worker)

    # The worker stopped it, after a second of its 5, and queued it again before it ended:
    # it's pending, without a run, and without a result to save.
    log = worker.log.read_text()
    assert f"Task {task_id} didn't end before its worker stopped: queued again, after 1 of 2 runs." in log
    # In a line, without TaskIQ's traceback of the CancelledError that stopped it.
    assert "CancelledError" not in log
    assert await run(retry_db, task_id) == ("PENDING", None, 1)
    assert not await has_result(retry_server, task_id)

    # The next worker runs it from the start.
    other = start_worker(retry_server)
    assert await wait_for_row(retry_db, task_id, "SUCCESS") == ("SUCCESS", 2.0)
    assert await attempts(retry_db, task_id) == 2
    assert "lost its worker" not in other.log.read_text()
    assert await retry_account.credits() == 4


async def test_a_task_stopped_in_its_last_run_fails(retry_server, start_worker, retry_db, retry_account):
    task_id = None
    for _ in range(2):  # retry_server's attempts
        worker = start_worker(retry_server, "--wait-tasks-timeout", "1")
        task_id = task_id or await start_divide(retry_server, retry_account)
        await wait_for_log(worker.log, f"Executing task divide with ID: {task_id}")
        stop(worker)
        await stopped(worker)

    assert await task_row(retry_db, task_id) == ("FAILURE", {**ERROR, "error": "TaskLost"})
    assert f"Task {task_id} didn't end before its worker stopped, in the last of its 2 runs: failed." in (
        worker.log.read_text()
    )
    assert await retry_account.credits() == 5


@pytest.fixture
async def waiting(app_module, services):
    """
    The worker's broker, with the app's Redis options, waiting for a task as
    TaskIQ makes it wait, on a queue of its own, through a Proxy to Redis:
    `popping` is the asyncio task that waits, which TaskIQ cancels when the
    worker stops, and `queue` the queue, in `redis`, a client of Redis itself.
    """
    from taskiq_task import REDIS_OPTIONS, Broker

    host, port = services.redis_url.removeprefix("redis://").split("/")[0].rsplit(":", 1)
    proxy = Proxy(host, int(port))
    queue = f"test-{uuid.uuid4().hex}"
    broker = Broker(f"redis://{proxy.address[0]}:{proxy.address[1]}/0", queue_name=queue, **REDIS_OPTIONS)
    redis = Redis.from_url(services.redis_url)
    listening = broker.listen()
    popping = asyncio.create_task(anext(listening))
    # Its BRPOP waits for a task, for a second.
    await asyncio.sleep(0.3)
    try:
        yield SimpleNamespace(popping=popping, proxy=proxy, queue=queue, redis=redis)
    finally:
        # Ends the connection of a BRPOP that's still waiting.
        proxy.stop()
        await asyncio.wait({popping}, timeout=5)
        await listening.aclose()
        await broker.shutdown()
        await redis.aclose()


async def test_a_task_redis_gives_a_stopping_worker_isnt_lost(waiting):
    # Redis takes the task from the queue for the BRPOP, but the reply doesn't arrive yet.
    waiting.proxy.hang()
    await waiting.redis.lpush(waiting.queue, "task")
    await asyncio.sleep(0.2)
    waiting.popping.cancel()
    await asyncio.sleep(0.2)
    waiting.proxy.resume()

    await asyncio.wait({waiting.popping}, timeout=5)
    assert not waiting.popping.cancelled(), "The task that Redis gave it was lost"
    assert waiting.popping.result() == b"task"
    assert await waiting.redis.llen(waiting.queue) == 0


async def test_a_stopping_worker_doesnt_wait_for_a_redis_that_hangs(waiting):
    waiting.proxy.hang()
    waiting.popping.cancel()
    # It waits 5 seconds for the BRPOP's reply, and then it stops, though redis-py's
    # reads time out after 5 seconds, and REDIS_OPTIONS retries them twice.
    await asyncio.wait({waiting.popping}, timeout=8)
    assert waiting.popping.cancelled()


def test_compose_gives_the_worker_time_to_stop():
    compose = (REPO / "compose.yaml").read_text()
    wait = int(re.search(r"--wait-tasks-timeout (\d+)", compose)[1])
    grace = int(re.search(r"stop_grace_period: (\d+)s", compose)[1])
    # Docker kills it after stop_grace_period: before that, it also stops the tasks and queues them again.
    assert grace >= wait + 20
