"""
Deleting an account, from the panel: its tasks go with it, its sessions and
its API key stop working, and logging in again starts another account,
without the initial credits. The worker skips the account's queued tasks,
and ends the one it was running without errors.
"""
import asyncio
import hashlib
import time
import tomllib
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient
from fastapi_sso.sso.base import OpenID
from taskiq.serializers import JSONSerializer
from taskiq_redis import RedisAsyncResultBackend

from tests.helpers import (
    client_for, cookie_attributes, create_account, insert_task, session_cookie, skipped, wait_for_lock, wait_for_log
)

pytestmark = pytest.mark.anyio


async def test_deleting_the_account_deletes_its_tasks_and_ends_its_sessions_and_api_key(
    server, db, account, user, anon
):
    task_ids = [await insert_task(db, account), await insert_task(db, account, status="SUCCESS", result=2.0)]
    # Its session on another device.
    other_device = session_cookie(account.google_id, version=0)

    r = await user.post("/user/delete_account")
    assert (r.status_code, r.headers["location"]) == (303, "/account_deleted")
    assert {"max-age=0", "httponly", "secure", "samesite=lax"} <= cookie_attributes(r.headers["set-cookie"])
    assert "Your account was deleted" in (await anon.get("/account_deleted")).text

    assert await db.fetchval("SELECT count(*) FROM account WHERE id = $1", account.id) == 0
    assert await db.fetchval("SELECT count(*) FROM taskhistory WHERE task_id = ANY($1)", task_ids) == 0
    for cookie in (account.cookie, other_device):
        async with client_for(server, cookie) as client:
            assert (await client.get("/user/credits")).status_code == 401
            assert (await client.get("/userpanel")).headers["location"] == "/"
    assert (await anon.get("/api/user_credits", headers={"X-API-Key": account.api_key})).status_code == 401

    # All that's kept: the hash of its Google ID.
    google_id_hash = hashlib.sha256(account.google_id.encode()).hexdigest()
    assert await db.fetchval("SELECT count(*) FROM deletedaccount WHERE google_id_hash = $1", google_id_hash) == 1


async def test_the_panel_deletes_the_account_with_a_post(user):
    # With GET, a link from another site could delete it.
    assert '<form method="post" action="/user/delete_account"' in (await user.get("/userpanel")).text
    assert (await user.get("/user/delete_account")).status_code == 405


def test_logging_in_again_starts_another_account_without_the_initial_credits(app_module, monkeypatch):
    google_id = f"test-{uuid.uuid4()}"

    async def verify_and_process(request, **kwargs):
        return OpenID(
            id=google_id,
            email=f"{google_id}@example.com",
            first_name="Test",
            last_name="User",
            display_name="Test User",
            picture="https://example.com/picture.png",
            provider="google",
        )

    monkeypatch.setattr(app_module.sso, "verify_and_process", verify_and_process)
    # Like a server that gives credits to new accounts.
    monkeypatch.setattr(app_module, "INITIAL_CREDITS", 25)

    with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
        def log_in():
            r = client.get("/auth/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
            # The client won't send a Secure cookie over http: set it by hand.
            client.cookies.clear()
            client.cookies.set("token", r.cookies["token"])
            return r.cookies["token"]

        first_session = log_in()
        assert client.get("/user/credits").json() == {"credits": 25}
        assert client.post("/user/delete_account", follow_redirects=False).status_code == 303

        log_in()
        assert client.get("/user/credits").json() == {"credits": 0}

        # The deleted account's session doesn't work in the new one, though both have the first version.
        client.cookies.clear()
        client.cookies.set("token", first_session)
        assert client.get("/user/credits").status_code == 401


async def test_a_queued_task_of_a_deleted_account_does_not_run(services, start_server, start_worker, db):
    # On its own Redis database, so only this server's worker gets the task.
    server = start_server("delete-queued", broker=services.redis_url.rsplit("/", 1)[0] + "/5")
    account = await create_account(db)
    async with client_for(server, account.cookie) as user:
        # There's no worker yet: the task waits in the queue while the account is deleted.
        task_id = (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]
        assert (await user.post("/user/delete_account")).status_code == 303

    worker = start_worker(server)
    await wait_for_log(worker.log, skipped(task_id))


async def wait_until_started(db, task_id, timeout=30):
    """Wait until a worker started the task (see TaskHistory.start())."""
    deadline = time.monotonic() + timeout
    while not await db.fetchval("SELECT started_at IS NOT NULL FROM taskhistory WHERE task_id = $1", task_id):
        if time.monotonic() > deadline:
            raise TimeoutError(f"No worker started task {task_id}")
        await asyncio.sleep(0.1)


async def test_a_task_running_when_its_account_is_deleted_ends_without_errors(
    services, start_server, start_worker, db
):
    # With a lease of 3 seconds, which the worker tries to renew every second.
    server = start_server("delete-running", broker=services.redis_url.rsplit("/", 1)[0] + "/6", lease=3)
    worker = start_worker(server)
    account = await create_account(db)
    async with client_for(server, account.cookie) as user:
        task_id = (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]
        # Not when TaskIQ logs "Executing task", which comes before the Receiver starts it.
        await wait_until_started(db, task_id)
        assert (await user.post("/user/delete_account")).status_code == 303

    # divide takes 5 seconds: the worker can't renew its lease, nor save it when it ends.
    backend = tomllib.loads(server.config.read_text())["taskiq"]["backend"]
    results = RedisAsyncResultBackend(backend, serializer=JSONSerializer(), prefix_str="nanosaas-result")
    try:
        deadline = time.monotonic() + 30
        while not await results.is_result_ready(task_id):
            assert time.monotonic() < deadline, "The task didn't end"
            await asyncio.sleep(0.2)
        assert (await results.get_result(task_id)).return_value == 2.0
    finally:
        await results.shutdown()

    log = worker.log.read_text()
    assert f"Error saving task {task_id}" not in log
    assert f"Can't renew the lease of task {task_id}" not in log
    assert "Traceback" not in log


async def test_deleting_an_account_waits_for_a_task_that_is_ending(services, db, account, user):
    task_id = await insert_task(db, account)
    ending = await asyncpg.connect(services.dsn)
    try:
        async with ending.transaction():
            # Like TaskHistory.finish() of a failed task: it locks the task's row...
            await ending.execute("UPDATE taskhistory SET status = 'FAILURE' WHERE task_id = $1", task_id)
            deleting = asyncio.create_task(user.post("/user/delete_account"))
            await wait_for_lock(db, 'DELETE FROM "taskhistory"')
            # ...and then the account's, to give the credit back. The deletion waits
            # for the task's row before it locks the account's, so there's no deadlock.
            await asyncio.wait_for(
                ending.execute("UPDATE account SET credits = credits + 1 WHERE id = $1", account.id), 5
            )
        assert (await deleting).status_code == 303
    finally:
        await ending.close()
    assert await db.fetchval("SELECT count(*) FROM account WHERE id = $1", account.id) == 0
