"""
Canceling a task that no worker started: its credit comes back, it stays in
the history as CANCELED, and the worker that takes it from the queue skips
it. A task that started, or ended, can't be canceled.
"""
import asyncio

import asyncpg
import pytest

from tests.helpers import client_for, create_account, insert_task, skipped, task_row, wait_for_lock, wait_for_log

pytestmark = pytest.mark.anyio

STARTED = "The task already started: only a task that no worker started can be canceled"


async def start(db, task_id):
    """Like TaskHistory.start(), in a worker, with a lease for a day: no worker queues it again."""
    await db.execute(
        "UPDATE taskhistory SET started_at = now(), attempts = 1, lease_until = now() + interval '1 day' "
        "WHERE task_id = $1 AND status = 'PENDING' AND started_at IS NULL",
        task_id,
    )


async def test_a_queued_task_is_canceled_and_its_worker_skips_it(services, start_server, start_worker, db, account):
    # On its own Redis database, and without a worker yet: the task waits in the queue.
    server = start_server("cancel-queued", broker=services.redis_url.rsplit("/", 1)[0] + "/7")
    async with client_for(server, account.cookie) as user:
        task_id = (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]
        assert await account.credits() == 4
        # The panel shows a Cancel button until a worker starts it.
        [task] = (await user.get("/user/task_history")).json()["tasks"]
        assert (task["status"], task["started"]) == ("PENDING", False)

        r = await user.post(f"/task/cancel/{task_id}")
        assert (r.status_code, r.json()) == (200, {"id": task_id, "status": "CANCELED"})
        [task] = (await user.get("/user/task_history")).json()["tasks"]
        assert task["status"] == "CANCELED"
    assert await account.credits() == 5

    worker = start_worker(server)
    await wait_for_log(worker.log, skipped(task_id))
    assert await task_row(db, task_id) == ("CANCELED", None)
    assert await account.credits() == 5


async def test_canceling_a_task_again_gives_its_credit_back_once(db, account, anon):
    # Pending, with its credit taken, like start_task() leaves it.
    task_id = await insert_task(db, account)
    await account.set_credits(4)

    # With the API key, many times at once.
    headers = {"X-API-Key": account.api_key}
    rs = await asyncio.gather(*(anon.post(f"/api/task/cancel/{task_id}", headers=headers) for _ in range(5)))
    assert [(r.status_code, r.json()) for r in rs] == [(200, {"id": task_id, "status": "CANCELED"})] * 5
    assert await account.credits() == 5


async def test_a_task_that_started_or_ended_cant_be_canceled(db, account, user):
    running = await insert_task(db, account)
    await start(db, running)
    ended = await insert_task(db, account, status="SUCCESS", result=2.0)

    for task_id, row in [(running, ("PENDING", None)), (ended, ("SUCCESS", 2.0))]:
        r = await user.post(f"/task/cancel/{task_id}")
        assert (r.status_code, r.json()) == (409, {"detail": STARTED})
        assert await task_row(db, task_id) == row
    assert await account.credits() == 5


async def test_another_accounts_task_cant_be_canceled(db, user):
    task_id = await insert_task(db, await create_account(db))
    r = await user.post(f"/task/cancel/{task_id}")
    assert (r.status_code, r.json()) == (404, {"detail": "Task not found or you do not have permission to access it"})
    assert await task_row(db, task_id) == ("PENDING", None)


async def test_a_worker_that_starts_the_task_meanwhile_wins(services, db, account, user):
    task_id = await insert_task(db, account)
    worker = await asyncpg.connect(services.dsn)
    try:
        async with worker.transaction():
            await start(worker, task_id)
            # The cancel waits for the worker's transaction, and then finds the task started.
            canceling = asyncio.create_task(user.post(f"/task/cancel/{task_id}"))
            await wait_for_lock(db, 'UPDATE "taskhistory"')
        r = await canceling
    finally:
        await worker.close()

    assert (r.status_code, r.json()) == (409, {"detail": STARTED})
    assert await task_row(db, task_id) == ("PENDING", None)
    assert await account.credits() == 5
