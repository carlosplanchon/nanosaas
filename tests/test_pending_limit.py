"""
The limit of pending tasks per account, MAX_PENDING_TASKS: with that many
waiting in the queue or running, an account can't start another until one
of them ends, so it can't fill the queue the workers take every task from.
"""
import asyncio

import asyncpg
import pytest

from tests.helpers import client_for, insert_task, wait_for_lock

pytestmark = pytest.mark.anyio

# limited_server's MAX_PENDING_TASKS.
LIMIT = 3
TOO_MANY = f"Too many pending tasks: an account can have up to {LIMIT}. Wait for one to end."
DIVIDE = {"x": 6, "y": 3}


async def pending(db, account):
    return await db.fetchval(
        "SELECT count(*) FROM taskhistory WHERE user_id = $1 AND status = 'PENDING'", account.id
    )


def test_the_limit_is_10_by_default(app_module):
    assert app_module.MAX_PENDING_TASKS == 10


async def test_an_account_with_the_most_pending_tasks_cant_start_another(limited_server, db, account):
    await account.set_credits(10)
    # Two pending already, and a third one started now: the most it can have.
    await insert_task(db, account)
    ending = await insert_task(db, account)
    api_key = {"X-API-Key": account.api_key}
    async with client_for(limited_server, account.cookie) as client:
        third = await client.post("/api/task/divide", json=DIVIDE, headers=api_key)
        assert third.status_code == 200

        # One more gets a 429 error, from the API or the panel, without spending a credit.
        for r in [await client.post("/api/task/divide", json=DIVIDE, headers=api_key),
                  await client.post("/task/divide", json=DIVIDE)]:
            assert (r.status_code, r.json()) == (429, {"detail": TOO_MANY})
        assert (await pending(db, account), await account.credits()) == (3, 9)

        # A task that ends makes room for another, and so does one that's canceled.
        await db.execute("UPDATE taskhistory SET status = 'SUCCESS' WHERE task_id = $1", ending)
        assert (await client.post("/task/divide", json=DIVIDE)).status_code == 200
        assert (await client.post("/task/divide", json=DIVIDE)).status_code == 429
        assert (await client.post(f"/task/cancel/{third.json()['id']}")).status_code == 200
        assert (await client.post("/task/divide", json=DIVIDE)).status_code == 200
    assert (await pending(db, account), await account.credits()) == (3, 8)


async def test_a_retry_gets_its_task_at_the_limit(limited_server, db, account):
    headers = {"X-API-Key": account.api_key, "Idempotency-Key": "first"}
    async with client_for(limited_server) as client:
        first = await client.post("/api/task/divide", json=DIVIDE, headers=headers)
        await insert_task(db, account)
        await insert_task(db, account)
        retry = await client.post("/api/task/divide", json=DIVIDE, headers=headers)
        another = await client.post("/api/task/divide", json=DIVIDE, headers={**headers, "Idempotency-Key": "second"})
    assert (first.status_code, retry.status_code, retry.json()["id"]) == (200, 200, first.json()["id"])
    assert (another.status_code, another.json()) == (429, {"detail": TOO_MANY})


async def test_requests_at_once_dont_pass_the_limit(services, limited_server, db, account):
    await insert_task(db, account)
    await insert_task(db, account)
    api_key = {"X-API-Key": account.api_key}
    holder = await asyncpg.connect(services.dsn)
    try:
        async with client_for(limited_server) as client:
            async with holder.transaction():
                # The account is held, so the four requests wait for it at once.
                await holder.execute("SELECT 1 FROM account WHERE id = $1 FOR UPDATE", account.id)
                requests = [
                    asyncio.create_task(client.post("/api/task/divide", json=DIVIDE, headers=api_key))
                    for _ in range(4)
                ]
                await wait_for_lock(db, '"account"', count=4)
            rs = await asyncio.gather(*requests)
    finally:
        await holder.close()

    # They count the pending tasks one at a time: only the first has room.
    assert sorted(r.status_code for r in rs) == [200, 429, 429, 429]
    assert await pending(db, account) == LIMIT
