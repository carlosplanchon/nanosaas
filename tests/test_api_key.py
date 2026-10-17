"""
The API key: in the X-API-Key header, or in the query for older clients.
Only its hash is stored.
"""
import asyncio
import time
from pathlib import Path

import pytest

from tests.helpers import stored_api_key

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent

# Each API route, with what it takes besides the key.
API_REQUESTS = (
    ("GET", "/api/user_credits", {}),
    ("POST", "/api/task/divide", {"json": {"x": 1, "y": 1}}),
    ("GET", "/api/task_details/no-such-task", {}),
)


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


async def test_only_a_hash_of_a_new_key_is_stored(db, account, user, anon):
    r = await user.post("/user/api_key")
    new_key = r.json()["api_key"]
    stored = await db.fetchval("SELECT api_key FROM account WHERE id = $1", account.id)
    assert stored == stored_api_key(new_key)
    assert new_key not in stored

    # The new key works, and the one it replaced doesn't anymore.
    r = await anon.get("/api/user_credits", headers={"X-API-Key": new_key})
    assert r.status_code == 200
    r = await anon.get("/api/user_credits", headers={"X-API-Key": account.api_key})
    assert r.status_code == 401


async def test_the_api_key_page_only_gets_the_start_of_the_key(account, user):
    r = await user.get("/user/api_key")
    assert r.json() == {"api_key_prefix": account.api_key[:8]}


async def test_keys_from_before_hashing_work_after_the_migration_script(db, account, anon):
    # An account from before the hashing: its key stored as is.
    await db.execute("UPDATE account SET api_key = $1 WHERE id = $2", account.api_key, account.id)
    headers = {"X-API-Key": account.api_key}
    assert (await anon.get("/api/user_credits", headers=headers)).status_code == 401

    script = (REPO / "scripts" / "hash_api_keys.sql").read_text()
    await db.execute(script)
    await db.execute(script)  # running it again is harmless

    stored = await db.fetchval("SELECT api_key FROM account WHERE id = $1", account.id)
    assert stored == stored_api_key(account.api_key)
    assert (await anon.get("/api/user_credits", headers=headers)).status_code == 200


async def test_api_keys_are_unique_and_indexed(db):
    # The same constraint the README says to add to databases created before.
    indexdef = await db.fetchval(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'account' AND indexname = 'account_api_key_key'"
    )
    assert indexdef is not None
    assert "UNIQUE" in indexdef and "(api_key)" in indexdef
