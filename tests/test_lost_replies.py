"""
Redis can run the LPUSH that queues a task and lose its reply. Then redis-py
sends the LPUSH again, queueing the task twice, or, without any reply, the
API gives the task up while Redis has it queued. Either way the task runs
once at most, and its credit is charged once at most.
"""
import asyncio
import contextlib
import re
import threading
import time
import uuid

import pytest

from tests.helpers import client_for, create_account, wait_for_row

pytestmark = pytest.mark.anyio


class LossyRedis:
    """
    A proxy to Redis that loses its replies to the next `drops` LPUSH
    commands: Redis runs them, but the client never hears back, as when
    the connection breaks at the wrong time.
    """

    def __init__(self, redis_url):
        host, port = redis_url.removeprefix("redis://").rsplit("/", 1)[0].rsplit(":", 1)
        self.redis = (host, int(port))
        self.drops = 0
        self.lpushes = 0  # The LPUSH commands that reached Redis.
        # On a thread of its own, so it answers while the test waits for something.
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.server = asyncio.run_coroutine_threadsafe(
            asyncio.start_server(self.relay, "127.0.0.1", 0), self.loop
        ).result()
        # Its own Redis database, so the other tests' workers don't take its tasks.
        self.url = f"redis://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/2"

    def close(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()

    async def relay(self, client_reader, client_writer):
        redis_reader, redis_writer = await asyncio.open_connection(*self.redis)

        async def replies():
            with contextlib.suppress(ConnectionError):
                while data := await redis_reader.read(65536):
                    client_writer.write(data)
                    await client_writer.drain()

        relaying = asyncio.create_task(replies())
        try:
            while data := await client_reader.read(65536):
                if b"LPUSH" in data.upper():
                    self.lpushes += 1
                    if self.drops > 0:
                        self.drops -= 1
                        relaying.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await relaying
                        redis_writer.write(data)
                        await redis_writer.drain()
                        await redis_reader.read(65536)  # Redis ran it: here's its reply,
                        return  # which the client never gets.
                redis_writer.write(data)
                await redis_writer.drain()
        except ConnectionError:
            pass
        finally:
            relaying.cancel()
            redis_writer.close()
            client_writer.close()


@pytest.fixture
def lossy_redis(services):
    proxy = LossyRedis(services.redis_url)
    yield proxy
    proxy.close()


def skips(worker, task_id):
    return worker.log.read_text().count(f"Task {task_id} was started already, or given up by the API: skipped.")


def runs(worker, task_id):
    """How many times the worker ran the task: TaskIQ logs every copy it takes, even the skipped ones."""
    return worker.log.read_text().count(f"Executing task divide with ID: {task_id}") - skips(worker, task_id)


async def wait_for_skips(worker, task_id, count, timeout=30):
    deadline = time.monotonic() + timeout
    while skips(worker, task_id) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.2)


@pytest.mark.parametrize(
    ("x", "y", "row", "credits"),
    [
        (6, 3, ("SUCCESS", 2.0), 4),
        # When divide gave its credit back itself, each copy that ran did.
        (1, 0, ("FAILURE", {"status": "error", "message": "Can't divide by zero.", "error": "TaskError"}), 5),
    ],
    ids=["success", "failure"],
)
async def test_a_task_queued_twice_runs_once(lossy_redis, start_server, start_worker, db, x, y, row, credits):
    server = start_server("lossy", broker=lossy_redis.url)
    worker = start_worker(server)
    account = await create_account(db)

    # Redis queues the task, but its reply is lost: redis-py sends the LPUSH again.
    lossy_redis.drops = 1
    async with client_for(server, account.cookie) as user:
        r = await user.post("/task/divide", json={"x": x, "y": y})
    assert r.status_code == 200
    task_id = r.json()["id"]
    assert lossy_redis.lpushes == 2

    assert await wait_for_row(db, task_id, row[0]) == row
    await wait_for_skips(worker, task_id, 1)
    assert (runs(worker, task_id), skips(worker, task_id)) == (1, 1)
    assert await account.credits() == credits


async def test_a_task_given_up_does_not_run_though_redis_queued_it(lossy_redis, start_server, start_worker, db):
    server = start_server("lossy", broker=lossy_redis.url)
    account = await create_account(db)

    # No reply to the LPUSH, nor to redis-py's 2 retries: the API gives the task up.
    lossy_redis.drops = 3
    async with client_for(server, account.cookie) as user:
        r = await user.post("/task/divide", json={"x": 6, "y": 3})
    assert (r.status_code, r.json()) == (500, {"detail": "Task initiation failed"})
    log = (server.config.parent / "uvicorn.log").read_text()
    task_id = re.findall(r"Task initiation failed for ([0-9a-f-]{36})", log)[-1]
    assert await db.fetchval("SELECT count(*) FROM taskhistory WHERE task_id = $1", task_id) == 0
    assert await account.credits() == 5

    # Redis queued it three times, but no copy runs.
    assert lossy_redis.lpushes == 3
    worker = start_worker(server)
    await wait_for_skips(worker, task_id, 3)
    assert (runs(worker, task_id), skips(worker, task_id)) == (0, 3)
    assert await account.credits() == 5


async def test_a_request_given_up_starts_its_task_when_sent_again_with_its_key(
    lossy_redis, start_server, start_worker, db
):
    server = start_server("lossy", broker=lossy_redis.url)
    account = await create_account(db)
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    async with client_for(server, account.cookie) as user:
        lossy_redis.drops = 3
        r = await user.post("/task/divide", json={"x": 6, "y": 3}, headers=headers)
        assert r.status_code == 500
        # The API gave the task up, so its key has no task yet: sent again, the request starts one.
        r = await user.post("/task/divide", json={"x": 6, "y": 3}, headers=headers)
    assert r.status_code == 200

    start_worker(server)
    assert await wait_for_row(db, r.json()["id"], "SUCCESS") == ("SUCCESS", 2.0)
    assert await account.credits() == 4
    assert await db.fetchval("SELECT count(*) FROM taskhistory WHERE user_id = $1", account.id) == 1


async def test_a_task_a_worker_started_is_not_given_up(lossy_redis, start_server, start_worker, db):
    server = start_server("lossy", broker=lossy_redis.url)
    worker = start_worker(server)
    account = await create_account(db)

    # The API never gets a reply, but the worker starts the first copy while redis-py waits to retry.
    lossy_redis.drops = 3
    async with client_for(server, account.cookie) as user:
        r = await user.post("/task/divide", json={"x": 6, "y": 3})
    assert r.status_code == 200
    task_id = r.json()["id"]
    assert lossy_redis.lpushes == 3

    assert await wait_for_row(db, task_id, "SUCCESS") == ("SUCCESS", 2.0)
    await wait_for_skips(worker, task_id, 2)
    assert (runs(worker, task_id), skips(worker, task_id)) == (1, 2)
    assert await account.credits() == 4
