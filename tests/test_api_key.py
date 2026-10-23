"""
The API keys: in the X-API-Key header, or in the query for older clients.
An account can have several, each with a name, and revoke each one on its
own. Only their hashes are stored.
"""
import asyncio
import datetime
import time

import asyncpg
import pytest

from tests.helpers import client_for, create_account, stored_api_key, wait_for_lock

pytestmark = pytest.mark.anyio

# Each API route, with what it takes besides the key.
API_REQUESTS = (
    ("GET", "/api/user_credits", {}),
    ("POST", "/api/task/divide", {"json": {"x": 1, "y": 1}}),
    ("GET", "/api/task_details/no-such-task", {}),
)


async def create_key(user, name="New key"):
    """Create a key from the API key page: its answer, with the whole key, and the account's keys."""
    r = await user.post("/user/api_keys", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


async def keys_of(user):
    """The account's keys, as the API key page gets them."""
    r = await user.get("/user/api_keys")
    assert r.status_code == 200, r.text
    return r.json()["api_keys"]


async def works(anon, key):
    """Whether the API takes a key."""
    r = await anon.get("/api/user_credits", headers={"X-API-Key": key})
    assert r.status_code in (200, 401), r.text
    return r.status_code == 200


def utc(text):
    return datetime.datetime.fromisoformat(text)


async def test_the_api_routes_take_the_key_in_the_header(account, anon):
    headers = {"X-API-Key": account.api_key}
    r = await anon.get("/api/user_credits", headers=headers)
    assert r.json() == {"credits": 5}

    r = await anon.post("/api/task/divide", json={"x": 10, "y": 4}, headers=headers)
    assert r.status_code == 200
    # The credit is reserved when the task is queued.
    assert await account.credits() == 4

    task_id = r.json()["id"]
    r = await anon.get(f"/api/task_details/{task_id}", headers=headers)
    assert (r.status_code, r.json()["task_id"]) == (200, task_id)


@pytest.mark.parametrize("where", ["header", "query"])
async def test_a_wrong_api_key_gives_401(anon, where):
    if where == "header":
        wrong = {"headers": {"X-API-Key": "wrong"}}
    else:
        wrong = {"params": {"api_key": "wrong"}}
    for method, path, payload in API_REQUESTS:
        assert (await anon.request(method, path, **payload, **wrong)).status_code == 401, path


async def test_a_missing_api_key_gives_401(anon):
    # When the key was a required query parameter, this gave a 422.
    for method, path, payload in API_REQUESTS:
        assert (await anon.request(method, path, **payload)).status_code == 401, path


async def test_the_header_takes_precedence_over_the_query(account, anon):
    r = await anon.get(
        "/api/user_credits", headers={"X-API-Key": account.api_key}, params={"api_key": "wrong"}
    )
    assert r.status_code == 200
    r = await anon.get(
        "/api/user_credits", headers={"X-API-Key": "wrong"}, params={"api_key": account.api_key}
    )
    assert r.status_code == 401


async def test_a_key_in_the_header_stays_out_of_the_server_log(server, account, anon):
    log = server.config.parent / "uvicorn.log"
    headers = {"X-API-Key": account.api_key}
    r = await anon.get("/api/user_credits", params={"probe": "header"}, headers=headers)
    assert r.status_code == 200
    # A key in the URL does end up in the log: that's why the header is preferred.
    await anon.get("/api/user_credits", params={"api_key": account.api_key})

    deadline = time.monotonic() + 5
    while f"api_key={account.api_key}" not in log.read_text() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    lines = log.read_text().splitlines()
    assert any(f"api_key={account.api_key}" in line for line in lines)
    header_line = next(line for line in lines if "probe=header" in line)
    assert account.api_key not in header_line


async def test_an_account_has_several_keys_each_with_its_name(db, account, user, anon):
    first = await create_key(user, "Production")
    second = await create_key(user, "  Staging server  ")
    whole = [account.api_key, first["api_key"], second["api_key"]]

    # The oldest first, each with its name, without the spaces around it, and the start of its key.
    keys = await keys_of(user)
    assert keys == second["api_keys"]
    assert [(key["name"], key["prefix"]) for key in keys] == [
        ("Test key", whole[0][:8]), ("Production", whole[1][:8]), ("Staging server", whole[2][:8]),
    ]
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    for key in keys[1:]:
        assert key["created_at"].endswith("Z")
        assert datetime.timedelta(0) <= now - utc(key["created_at"]) < datetime.timedelta(minutes=1)

    # Each one works, the one the account had too.
    for key in whole:
        assert await works(anon, key)

    # Only their hashes are stored: the whole key goes out once, when it's created.
    stored = await db.fetch("SELECT key_hash FROM apikey WHERE account_id = $1 ORDER BY id", account.id)
    assert [row["key_hash"] for row in stored] == [stored_api_key(key) for key in whole]
    page = (await user.get("/user/api_keys")).text
    assert not any(key in page for key in whole)


async def test_revoking_a_key_stops_only_that_one(account, user, anon):
    new = await create_key(user)
    [old] = [key for key in new["api_keys"] if key["prefix"] == account.api_key[:8]]

    r = await user.delete(f"/user/api_keys/{old['id']}")
    assert r.status_code == 200
    assert [key["prefix"] for key in r.json()["api_keys"]] == [new["api_key"][:8]]
    # Right away: the requests with it get a 401, and the other one keeps working.
    r = await anon.get("/api/user_credits", headers={"X-API-Key": account.api_key})
    assert (r.status_code, r.json()) == (401, {"detail": "Invalid API key"})
    assert await works(anon, new["api_key"])

    r = await user.delete(f"/user/api_keys/{old['id']}")
    assert (r.status_code, r.json()) == (404, {"detail": "API key not found"})


async def test_an_account_can_t_see_nor_revoke_another_s_keys(server, db, user, anon):
    other = await create_account(db)
    async with client_for(server, other.cookie) as other_user:
        [other_key] = await keys_of(other_user)
    assert other_key["id"] not in [key["id"] for key in await keys_of(user)]

    r = await user.delete(f"/user/api_keys/{other_key['id']}")
    assert (r.status_code, r.json()) == (404, {"detail": "API key not found"})
    assert await works(anon, other.api_key)


async def test_an_account_has_up_to_10_keys(user):
    # The one it had, and 9 more.
    for n in range(9):
        created = await create_key(user, f"Key {n}")
    r = await user.post("/user/api_keys", json={"name": "One too many"})
    assert (r.status_code, r.json()) == (409, {"detail": "An account can have up to 10 API keys: revoke one first."})
    assert len(await keys_of(user)) == 10

    # Revoking one makes room for another.
    assert (await user.delete(f"/user/api_keys/{created['api_keys'][-1]['id']}")).status_code == 200
    await create_key(user, "Another")
    assert len(await keys_of(user)) == 10


async def test_concurrent_requests_can_t_give_an_account_more_than_10_keys(services, db, account, user):
    for n in range(8):
        await create_key(user, f"Key {n}")
    # It has 9. Its new keys wait to be saved, as if the database were slow: without a
    # lock, each request would count 9 keys, and save its own. From a connection of its
    # own: in a transaction, pg_stat_activity keeps showing what it showed first.
    locker = await asyncpg.connect(services.dsn)
    try:
        async with locker.transaction():
            await locker.execute("LOCK TABLE apikey IN SHARE ROW EXCLUSIVE MODE")
            requests = [asyncio.create_task(user.post("/user/api_keys", json={"name": f"At once {n}"})) for n in range(4)]
            # The first one saving its key, and the others waiting for it, or all of them saving theirs.
            await wait_for_lock(db, "account", count=4)
    finally:
        await locker.close()
    rs = await asyncio.gather(*requests)
    assert sorted(r.status_code for r in rs) == [200, 409, 409, 409]
    assert await db.fetchval("SELECT count(*) FROM apikey WHERE account_id = $1", account.id) == 10


@pytest.mark.parametrize("name", ["", "   ", "x" * 51])
async def test_a_key_needs_a_name_of_up_to_50_characters(user, name):
    r = await user.post("/user/api_keys", json={"name": name})
    assert (r.status_code, r.json()) == (422, {"detail": "Give the key a name, of up to 50 characters."})
    assert len(await keys_of(user)) == 1


async def test_a_name_of_50_characters_is_fine(user):
    created = await create_key(user, "x" * 50)
    assert created["api_keys"][-1]["name"] == "x" * 50


async def test_the_page_shows_when_each_key_was_last_used(db, account, user, anon):
    new = await create_key(user)

    def last_used(keys, key):
        [found] = [item["last_used_at"] for item in keys if item["prefix"] == key[:8]]
        return found

    # Not used yet.
    keys = await keys_of(user)
    assert (last_used(keys, account.api_key), last_used(keys, new["api_key"])) == (None, None)

    before = datetime.datetime.now(tz=datetime.timezone.utc)
    assert await works(anon, account.api_key)
    keys = await keys_of(user)
    used = last_used(keys, account.api_key)
    assert used.endswith("Z") and datetime.timedelta(0) <= utc(used) - before < datetime.timedelta(minutes=1)
    # Only the key that was used.
    assert last_used(keys, new["api_key"]) is None

    # Within a minute, it isn't saved again: not a write to the database on each request.
    assert await works(anon, account.api_key)
    assert last_used(await keys_of(user), account.api_key) == used

    # A minute later, it is.
    await db.execute(
        "UPDATE apikey SET last_used_at = last_used_at - interval '61 seconds' WHERE key_hash = $1",
        stored_api_key(account.api_key),
    )
    assert await works(anon, account.api_key)
    assert utc(last_used(await keys_of(user), account.api_key)) > utc(used)


async def test_the_key_routes_take_only_the_session(account, anon):
    # Not even with an API key: a leaked one can't make more keys, nor revoke the others.
    headers = {"X-API-Key": account.api_key}
    for method, path, payload in [
        ("GET", "/user/api_keys", {}),
        ("POST", "/user/api_keys", {"json": {"name": "Mine now"}}),
        ("DELETE", "/user/api_keys/1", {}),
    ]:
        assert (await anon.request(method, path, headers=headers, **payload)).status_code == 401, (method, path)
    # Nor in /docs, which anyone can read.
    assert not [path for path in (await anon.get("/openapi.json")).json()["paths"] if path.startswith("/user/api_keys")]


async def test_api_keys_are_unique_and_indexed(db):
    # The API finds each key by its hash.
    indexdef = await db.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'apikey' AND indexdef LIKE '%(key_hash)'"
    )
    assert indexdef is not None and "UNIQUE" in indexdef
