"""
The worker's healthcheck, scripts/worker_healthcheck.py: each process of the
worker writes its heartbeat every 5 seconds, while it can use PostgreSQL and
Redis, and the check fails when one is late, as when a task's blocking code
holds the process's event loop.
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tortoise import Tortoise

from tests.helpers import Proxy, wait_for_log, wait_for_row

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "scripts" / "worker_healthcheck.py"


def healthcheck(heartbeats, limit=None):
    """The check on the heartbeats of a directory, as compose.yaml runs it: its exit code and its message."""
    args = [sys.executable, str(CHECK), *([] if limit is None else [str(limit)])]
    run = subprocess.run(args, env={**os.environ, "TMPDIR": str(heartbeats)}, capture_output=True, text=True)
    return run.returncode, run.stderr.strip()


async def start_in_process(app_module, account, task, parameters):
    """start_task() in this process: the API's routes only have the app's tasks."""
    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        row = await app_module.Account.get(google_id=account.google_id)
        return await app_module.start_task(row, task, parameters)
    finally:
        await Tortoise.close_connections()


async def first_heartbeat(worker):
    heartbeat = worker.heartbeats / "nanosaas-heartbeat-worker-0"
    deadline = time.monotonic() + 10
    while not heartbeat.exists():
        assert time.monotonic() < deadline, "The worker hasn't written its heartbeat"
        await asyncio.sleep(0.2)


async def started(db, task_id, timeout=30):
    deadline = time.monotonic() + timeout
    while not await db.fetchval("SELECT started_at IS NOT NULL FROM taskhistory WHERE task_id = $1", task_id):
        assert time.monotonic() < deadline, "No worker started the task"
        await asyncio.sleep(0.1)


def test_the_check_wants_a_recent_heartbeat_of_each_process(tmp_path):
    assert healthcheck(tmp_path) == (1, "No process of the worker has written its heartbeat")
    for name in ("worker-0", "worker-1"):
        (tmp_path / f"nanosaas-heartbeat-{name}").write_text("")
    assert healthcheck(tmp_path) == (0, "")

    late = time.time() - 40
    os.utime(tmp_path / "nanosaas-heartbeat-worker-1", (late, late))
    assert healthcheck(tmp_path) == (1, "No heartbeat for nanosaas-heartbeat-worker-1 in 40 seconds")


async def test_a_task_with_blocking_code_stops_the_heartbeat(app_module, db, account, worker, taskiq_client):
    from tests import worker_app

    await first_heartbeat(worker)
    assert healthcheck(worker.heartbeats, limit=6) == (0, "")

    # While its time.sleep() holds the event loop, the heartbeat doesn't come.
    task_id = (await start_in_process(app_module, account, worker_app.block, {"seconds": 12})).id
    await started(db, task_id)
    await asyncio.sleep(8)
    code, message = healthcheck(worker.heartbeats, limit=6)
    assert (code, message.startswith("No heartbeat for nanosaas-heartbeat-worker-0 in ")) == (1, True)

    # Once the task ends, it comes again.
    assert await wait_for_row(db, task_id, "SUCCESS") == ("SUCCESS", None)
    await asyncio.sleep(2)
    assert healthcheck(worker.heartbeats, limit=6) == (0, "")


async def test_without_postgresql_the_heartbeat_stops(services, start_server, start_worker):
    proxy = Proxy(services.db_host, services.db_port)
    try:
        # On its own Redis database, so this worker gets no other test's tasks.
        server = start_server(
            "heartbeat-postgres", db_address=proxy.address, broker=services.redis_url.rsplit("/", 1)[0] + "/10"
        )
        worker = start_worker(server)
        await first_heartbeat(worker)
        assert healthcheck(worker.heartbeats, limit=6) == (0, "")

        proxy.close()
        await asyncio.sleep(8)
        code, message = healthcheck(worker.heartbeats, limit=6)
        assert (code, message.startswith("No heartbeat for nanosaas-heartbeat-worker-0 in ")) == (1, True)
        await wait_for_log(worker.log, "Heartbeat stopped: PostgreSQL or Redis doesn't answer")
    finally:
        # Before the server and the worker stop, so they don't wait for its connections.
        proxy.stop()


async def test_without_redis_the_heartbeat_stops_and_the_log_says_why(services, start_server, start_worker):
    host, port = services.redis_url.removeprefix("redis://").split("/")[0].rsplit(":", 1)
    proxy = Proxy(host, int(port))
    try:
        # On its own Redis database, so this worker gets no other test's tasks.
        server = start_server("heartbeat-redis", broker=f"redis://{proxy.address[0]}:{proxy.address[1]}/12")
        worker = start_worker(server)
        await first_heartbeat(worker)

        proxy.close()
        # The error's type and message, with the address it can't reach.
        await wait_for_log(worker.log, "Heartbeat stopped: PostgreSQL or Redis doesn't answer: ConnectionError: ")
        [line] = [line for line in worker.log.read_text().splitlines() if "Heartbeat stopped" in line]
        assert f"connecting to {proxy.address[0]}:{proxy.address[1]}" in line, line
    finally:
        # Before the server and the worker stop, so they don't wait for its connections.
        proxy.stop()


def test_compose_checks_the_health_of_the_worker():
    compose = (REPO / "compose.yaml").read_text()
    assert 'test: ["CMD", "python", "scripts/worker_healthcheck.py"]' in compose
