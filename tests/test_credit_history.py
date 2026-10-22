"""
The credit history (CreditMovement): each change of an account's credits,
with the balance it left and why, in the panel and at /api/credit_history.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from fastapi_sso.sso.base import OpenID

from tests.helpers import create_account, movements, wait_for_row

pytestmark = pytest.mark.anyio


def summary(page):
    """The changes of a page of the history: (reason, amount, balance, task_id) each."""
    return [(m["reason"], m["amount"], m["balance"], m["task_id"]) for m in page["movements"]]


async def test_each_task_s_cost_and_refund_are_in_the_history(worker, db, account, user, anon):
    succeeded = (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]
    failed = (await user.post("/task/divide", json={"x": 1, "y": 0})).json()["id"]
    assert (await wait_for_row(db, succeeded, "SUCCESS"))[0] == "SUCCESS"
    assert (await wait_for_row(db, failed, "FAILURE"))[0] == "FAILURE"

    # The latest first: the failed task's credit came back in its transaction.
    expected = [("refund", 1, 4, failed), ("task", -1, 3, failed), ("task", -1, 4, succeeded)]
    page = (await user.get("/user/credit_history")).json()
    assert (summary(page), page["has_more"]) == (expected, False)
    assert await account.credits() == 4
    # The same, with the API key.
    r = await anon.get("/api/credit_history", headers={"X-API-Key": account.api_key})
    assert r.json() == page


async def test_concurrent_tasks_leave_a_history_that_adds_up(worker, db, account, user):
    await account.set_credits(3)
    responses = await asyncio.gather(*(user.post("/task/divide", json={"x": 1, "y": 1}) for _ in range(6)))
    assert sorted(r.status_code for r in responses) == [200] * 3 + [403] * 3

    # One change for each task that started, and each with its own balance.
    history = await movements(db, account)
    assert [(reason, amount, balance) for reason, amount, balance, _ in history] == [
        ("task", -1, 2), ("task", -1, 1), ("task", -1, 0),
    ]
    assert await account.credits() == 0


async def test_the_history_comes_a_page_at_a_time(db, account, user):
    # 25 changes, 5 at a time with the same time, so pages of 7 end among changes
    # as old as the next one: the id tells them apart. The latest has the balance 30.
    for i in range(25):
        await db.execute(
            "INSERT INTO creditmovement (account_id, amount, balance, reason, created_at) "
            "VALUES ($1, 1, $2, 'admin', date_trunc('second', now()) - $3 * interval '1 second')",
            account.id, 6 + i, (24 - i) // 5,
        )

    seen, before = [], None
    for more in (True, True, True, False):
        params = {"limit": 7} if before is None else {"limit": 7, "before": before}
        page = (await user.get("/user/credit_history", params=params)).json()
        assert page["has_more"] is more
        seen += page["movements"]
        before = page["movements"][-1]["id"]
    assert [movement["balance"] for movement in seen] == list(range(30, 5, -1))
    # A page that ends with the oldest says there are no more, even when it's full.
    page = (await user.get("/user/credit_history", params={"limit": 25})).json()
    assert (len(page["movements"]), page["has_more"]) == (25, False)

    # Another account's change doesn't start a page of this one.
    other = await create_account(db)
    other_id = await db.fetchval(
        "INSERT INTO creditmovement (account_id, amount, balance, reason, created_at) "
        "VALUES ($1, 1, 6, 'admin', now()) RETURNING id",
        other.id,
    )
    r = await user.get("/user/credit_history", params={"before": other_id})
    assert (r.status_code, r.json()) == (404, {"detail": "Credit movement not found"})
    assert (await user.get("/user/credit_history", params={"limit": 101})).status_code == 422


def login(app_module, monkeypatch, initial_credits):
    """Log in twice as a new Google account, with INITIAL_CREDITS: its credit history."""
    google_id = f"test-{uuid.uuid4()}"

    async def verify_and_process(request, **kwargs):
        return OpenID(
            id=google_id, email=f"{google_id}@example.com", first_name="Test", last_name="User",
            display_name="Test User", picture="https://example.com/picture.png", provider="google",
        )

    monkeypatch.setattr(app_module.sso, "verify_and_process", verify_and_process)
    monkeypatch.setattr(app_module, "INITIAL_CREDITS", initial_credits)
    with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
        # The second login finds the account: it doesn't get them again.
        for _ in range(2):
            r = client.get("/auth/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
            # The client won't send a Secure cookie over http: set it by hand.
            client.cookies.set("token", r.cookies["token"])
        return client.get("/user/credit_history").json()


def test_a_new_account_starts_its_history_with_its_initial_credits(app_module, monkeypatch):
    assert summary(login(app_module, monkeypatch, 25)) == [("initial", 25, 25, None)]


def test_an_account_without_initial_credits_starts_without_history(app_module, monkeypatch):
    assert summary(login(app_module, monkeypatch, 0)) == []


async def test_deleting_the_account_deletes_its_history(db, account, user):
    await db.execute(
        "INSERT INTO creditmovement (account_id, amount, balance, reason, created_at) "
        "VALUES ($1, 5, 5, 'admin', now())",
        account.id,
    )
    assert (await user.post("/user/delete_account")).status_code == 303
    assert await db.fetchval("SELECT count(*) FROM creditmovement WHERE account_id = $1", account.id) == 0
