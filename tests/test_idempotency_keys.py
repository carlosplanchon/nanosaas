"""
The Idempotency-Key header: a request sent again with the key of a task the
account started gets that task, without paying for another one.
"""
import asyncio
import uuid

import asyncpg
import pytest

from tests.helpers import create_account, insert_task

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("worker")]


def start(client, key, x=6, y=3, api_key=None):
    """Start divide with an Idempotency-Key, from the panel's route, or the API's with an API key."""
    headers = {"Idempotency-Key": key}
    if api_key:
        headers["X-API-Key"] = api_key
    path = "/api/task/divide" if api_key else "/task/divide"
    return client.post(path, json={"x": x, "y": y}, headers=headers)


async def tasks_of(db, account):
    return await db.fetchval("SELECT count(*) FROM taskhistory WHERE user_id = $1", account.id)


@pytest.mark.parametrize("route", ["panel", "api"])
async def test_a_request_sent_again_with_its_key_gets_the_same_task(route, db, account, user, anon):
    key = str(uuid.uuid4())
    client, api_key = (user, None) if route == "panel" else (anon, account.api_key)

    first = await start(client, key, api_key=api_key)
    again = await start(client, key, api_key=api_key)
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert await account.credits() == 4
    assert await tasks_of(db, account) == 1


async def test_requests_sent_at_once_with_the_same_key_start_one_task(db, account, user):
    # Like a double click, with the last credit: the second request gets the
    # task of the first one, instead of "Not enough credits".
    await account.set_credits(1)
    key = str(uuid.uuid4())
    rs = await asyncio.gather(*(start(user, key) for _ in range(5)))
    assert [r.status_code for r in rs] == [200] * 5
    assert len({r.json()["id"] for r in rs}) == 1
    assert await account.credits() == 0
    assert await tasks_of(db, account) == 1


async def test_a_key_used_for_other_parameters_gets_422(db, account, user):
    key = str(uuid.uuid4())
    assert (await start(user, key, x=6, y=3)).status_code == 200
    r = await start(user, key, x=6, y=2)
    assert (r.status_code, r.json()) == (
        422, {"detail": "This Idempotency-Key was used for another task or other parameters"}
    )
    assert await account.credits() == 4
    assert await tasks_of(db, account) == 1


async def test_each_account_has_its_own_keys(db, account, anon):
    other = await create_account(db)
    key = str(uuid.uuid4())
    rs = [await start(anon, key, api_key=owner.api_key) for owner in (account, other)]
    assert [r.status_code for r in rs] == [200, 200]
    assert rs[0].json()["id"] != rs[1].json()["id"]
    assert (await account.credits(), await other.credits()) == (4, 4)


async def test_the_database_keeps_one_task_per_account_and_key(db, account):
    first, second = await insert_task(db, account), await insert_task(db, account)
    set_key = "UPDATE taskhistory SET idempotency_key = 'k' WHERE task_id = $1"
    await db.execute(set_key, first)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(set_key, second)


@pytest.mark.parametrize("key", ["", "k" * 256], ids=["empty", "too-long"])
async def test_a_key_has_1_to_255_characters(key, db, account, user):
    r = await start(user, key)
    assert (r.status_code, r.json()["detail"][0]["loc"]) == (422, ["header", "Idempotency-Key"])
    assert await account.credits() == 5
    assert await tasks_of(db, account) == 0
