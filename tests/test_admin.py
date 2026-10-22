"""
The admin page: for the emails in ADMINS, to see the accounts, give them
credits and delete them. For anyone else, its routes don't exist.
"""
import asyncio
import hashlib

import pytest

from tests.helpers import ADMIN, ADMIN_EMAIL, client_for, insert_task, movements, session_cookie

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("worker")]

NOT_FOUND = (404, {"detail": "Not Found"})


@pytest.fixture
async def admin(server, db):
    """A client logged in as the tests' admin, whose email is in ADMINS."""
    await db.execute(
        """
        INSERT INTO account (google_id, email, picture, provider, created_at, credits)
        VALUES ($1, $2, '', 'google', now(), 0)
        ON CONFLICT (google_id) DO NOTHING
        """,
        ADMIN, ADMIN_EMAIL,
    )
    async with client_for(server, session_cookie(ADMIN)) as client:
        yield client


async def test_for_anyone_else_the_admin_routes_dont_exist(account, user, anon):
    # A user, logged in or not, gets what an unknown route gives.
    assert ((r := await anon.get("/no-such-page")).status_code, r.json()) == NOT_FOUND
    for client in (user, anon):
        for method, path, payload in [
            ("GET", "/admin", {}),
            ("GET", "/admin/accounts", {}),
            ("POST", f"/admin/accounts/{account.id}/credits", {"json": {"credits": 5}}),
            # Not even a 422 for a wrong body.
            ("POST", f"/admin/accounts/{account.id}/credits", {"json": {"credits": 0}}),
            ("DELETE", f"/admin/accounts/{account.id}", {}),
        ]:
            r = await client.request(method, path, **payload)
            assert (r.status_code, r.json()) == NOT_FOUND, path
    assert await account.credits() == 5

    # And /docs, which anyone can read, doesn't list them.
    paths = (await anon.get("/openapi.json")).json()["paths"]
    assert not [path for path in paths if path.startswith("/admin")]


async def test_an_admin_finds_an_account_and_gives_it_credits(admin, db, account):
    page = await admin.get("/admin")
    assert (page.status_code, "<title>Admin</title>" in page.text) == (200, True)

    await insert_task(db, account)
    r = await admin.get("/admin/accounts", params={"search": account.google_id})
    [found] = r.json()
    assert (found["id"], found["email"], found["credits"], found["pending_tasks"]) == (
        account.id, f"{account.google_id}@example.com", 5, 1
    )

    r = await admin.post(f"/admin/accounts/{account.id}/credits", json={"credits": 10})
    assert (r.status_code, r.json()) == (200, {"credits": 15})
    assert await account.credits() == 15
    assert await movements(db, account) == [("admin", 10, 15, None)]

    r = await admin.post(f"/admin/accounts/{account.id}/credits", json={"credits": 0})
    assert r.status_code == 422
    r = await admin.post("/admin/accounts/0/credits", json={"credits": 1})
    assert (r.status_code, r.json()) == (404, {"detail": "Account not found"})
    assert (await admin.get("/admin/accounts", params={"search": "no-such-email"})).json() == []


async def test_an_admin_deletes_an_account_with_its_tasks(admin, server, db, account, anon):
    task_id = await insert_task(db, account)

    r = await admin.delete(f"/admin/accounts/{account.id}")
    assert (r.status_code, r.json()) == (200, {"deleted": account.id})
    assert await db.fetchval("SELECT count(*) FROM account WHERE id = $1", account.id) == 0
    assert await db.fetchval("SELECT count(*) FROM taskhistory WHERE task_id = $1", task_id) == 0
    # As when its user deletes it: the session and the API key stop working, and the hash is kept.
    async with client_for(server, account.cookie) as user:
        assert (await user.get("/user/credits")).status_code == 401
    assert (await anon.get("/api/user_credits", headers={"X-API-Key": account.api_key})).status_code == 401
    google_id_hash = hashlib.sha256(account.google_id.encode()).hexdigest()
    assert await db.fetchval("SELECT count(*) FROM deletedaccount WHERE google_id_hash = $1", google_id_hash) == 1
    # Who deleted which one.
    assert f"{ADMIN_EMAIL} deleted account {account.id}" in (server.config.parent / "uvicorn.log").read_text()

    r = await admin.delete(f"/admin/accounts/{account.id}")
    assert (r.status_code, r.json()) == (404, {"detail": "Account not found"})


async def test_credits_given_while_tasks_start_add_up(admin, account, user):
    # Each task reserves a credit and each admin gives one, at once: none is lost.
    rs = await asyncio.gather(
        *(user.post("/task/divide", json={"x": 6, "y": 3}) for _ in range(5)),
        *(admin.post(f"/admin/accounts/{account.id}/credits", json={"credits": 1}) for _ in range(5)),
    )
    assert [r.status_code for r in rs] == [200] * 10
    assert await account.credits() == 5 + 5 - 5


async def test_the_panel_shows_admins_the_way_to_the_admin_page(admin, user):
    link = 'href="/admin"'
    assert link in (await admin.get("/userpanel")).text
    assert link not in (await user.get("/userpanel")).text
