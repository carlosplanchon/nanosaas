"""
Exporting an account's data, from the panel: a JSON file with its profile,
its webhook, without its secret, and its tasks, credit history and webhook
events, as the API and the API key page give them, the oldest first.
"""
import base64
import datetime
import json
import uuid

import pytest

from tests.helpers import age_task, client_for, create_account, insert_task

pytestmark = pytest.mark.anyio

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
# Past the deadline of a task that never started: the default queue_timeout, plus a minute.
OVERDUE = datetime.timedelta(seconds=600 + 60 + 60)
# More rows than the export reads at a time (EXPORT_PAGE in main_api.py).
MANY = 1234


async def export(server, account):
    """The account's export, as the panel's link downloads it: the response, and its JSON."""
    async with client_for(server, account.cookie) as user:
        r = await user.get("/user/export")
    assert r.status_code == 200, r.text
    return r, json.loads(r.content)


async def insert_movement(db, account, amount, balance, reason, task_id=None):
    await db.execute(
        "INSERT INTO creditmovement (account_id, amount, balance, reason, task_id, created_at) "
        "VALUES ($1, $2, $3, $4, $5, now())",
        account.id, amount, balance, reason, task_id,
    )


async def insert_webhook(db, account):
    return await db.fetchval(
        "INSERT INTO webhook (account_id, url, secret, created_at) "
        "VALUES ($1, 'https://example.com/hook?token=t', $2, now()) RETURNING id",
        account.id, SECRET,
    )


async def test_the_export_has_the_account_s_data_as_the_api_and_the_pages_give_it(server, db, account, anon):
    await db.execute("UPDATE account SET display_name = 'José Ñandú' WHERE id = $1", account.id)
    # The oldest first: a task that failed, one done, and one pending.
    error = {"status": "error", "message": "División por cero.", "error": "TaskError"}
    failed = await insert_task(db, account, status="FAILURE", result=error)
    await age_task(db, failed, datetime.timedelta(hours=2))
    done = await insert_task(db, account, status="SUCCESS", result=2.0)
    await age_task(db, done, datetime.timedelta(hours=1))
    pending = await insert_task(db, account)
    for amount, balance, reason, task_id in [(5, 5, "opening", None), (-1, 4, "task", done), (1, 5, "refund", failed)]:
        await insert_movement(db, account, amount, balance, reason, task_id)
    webhook_id = await insert_webhook(db, account)
    await db.execute(
        "INSERT INTO webhookdelivery (webhook_id, message_id, type, task_id, payload, created_at, status, attempts, "
        "next_attempt_at, last_response) VALUES ($1, $2, 'task.ended', $3, '{}', now(), 'DELIVERED', 1, now(), '200 OK')",
        webhook_id, f"msg_{uuid.uuid4().hex}", done,
    )
    # Another account's task, which isn't in it.
    others = await insert_task(db, await create_account(db), status="SUCCESS", result=1.0)

    r, data = await export(server, account)

    headers = {"X-API-Key": account.api_key}
    history = (await anon.get("/api/task_history", headers=headers)).json()["tasks"]
    assert data["tasks"] == history[::-1]
    assert [task["task_id"] for task in data["tasks"]] == [failed, done, pending]
    movements = (await anon.get("/api/credit_history", headers=headers)).json()["movements"]
    assert data["credit_history"] == movements[::-1]
    assert [movement["reason"] for movement in data["credit_history"]] == ["opening", "task", "refund"]
    async with client_for(server, account.cookie) as user:
        deliveries = (await user.get("/user/webhook")).json()["deliveries"]
    assert data["webhook_events"] == deliveries[::-1]
    assert others not in r.text

    # Without the webhook's secret, nor the API key: credentials, which the export doesn't need.
    created_at = await db.fetchval("SELECT created_at FROM webhook WHERE id = $1", webhook_id)
    assert data["webhook"] == {
        "url": "https://example.com/hook?token=t", "created_at": created_at.isoformat(),
        "disabled_at": None, "disabled_reason": None,
    }
    assert SECRET not in r.text and account.api_key not in r.text
    created_at = await db.fetchval("SELECT created_at FROM account WHERE id = $1", account.id)
    assert data["account"] == {
        "google_id": account.google_id, "email": f"{account.google_id}@example.com",
        "first_name": "Test", "last_name": "User", "display_name": "José Ñandú",
        "picture": "https://example.com/picture.png", "provider": "google",
        "created_at": created_at.isoformat(), "credits": 5, "api_key_prefix": account.api_key[:8],
    }
    # As it is, in UTF-8.
    assert "José Ñandú".encode() in r.content and "División por cero.".encode() in r.content
    exported_at = datetime.datetime.fromisoformat(data["exported_at"])
    assert datetime.timedelta(0) <= datetime.datetime.now(tz=datetime.timezone.utc) - exported_at < datetime.timedelta(minutes=1)


async def test_the_export_is_a_file_that_no_cache_keeps(server, account):
    before = datetime.datetime.now(tz=datetime.timezone.utc).date()
    r, _ = await export(server, account)
    after = datetime.datetime.now(tz=datetime.timezone.utc).date()
    assert r.headers["content-type"] == "application/json"
    names = {f'attachment; filename="nanosaas-account-{day}.json"' for day in (before, after)}
    assert r.headers["content-disposition"] in names
    assert r.headers["cache-control"] == "no-store"


async def test_an_account_without_tasks_nor_webhook(server, account):
    _, data = await export(server, account)
    assert (data["tasks"], data["credit_history"], data["webhook"], data["webhook_events"]) == ([], [], None, [])


async def test_the_export_sends_every_row_once_however_many(server, db, account):
    # All as old: the pages of the export's reads neither skip nor repeat the rows with the same time.
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    task_ids = [str(uuid.uuid4()) for _ in range(MANY)]
    await db.executemany(
        "INSERT INTO taskhistory (task_id, task_type, parameters, status, result, created_at, user_id, cost) "
        "VALUES ($1, 'divide', '{\"x\": 6, \"y\": 3}', 'SUCCESS', '2.0', $2, $3, 1)",
        [(task_id, now, account.id) for task_id in task_ids],
    )
    await db.executemany(
        "INSERT INTO creditmovement (account_id, amount, balance, reason, created_at) VALUES ($1, -1, $2, 'task', $3)",
        [(account.id, balance, now) for balance in range(MANY)],
    )
    webhook_id = await insert_webhook(db, account)
    message_ids = [f"msg_{uuid.uuid4().hex}" for _ in range(MANY)]
    await db.executemany(
        "INSERT INTO webhookdelivery (webhook_id, message_id, type, payload, created_at, status, attempts, "
        "next_attempt_at) VALUES ($1, $2, 'webhook.test', '{}', $3, 'DELIVERED', 1, $3)",
        [(webhook_id, message_id, now) for message_id in message_ids],
    )

    r, data = await export(server, account)
    # Sent as it's read, without holding it all in memory.
    assert r.headers["transfer-encoding"] == "chunked"
    assert [task["task_id"] for task in data["tasks"]] == task_ids
    assert [movement["balance"] for movement in data["credit_history"]] == list(range(MANY))
    assert [event["id"] for event in data["webhook_events"]] == message_ids


async def test_the_export_ends_the_lost_tasks_first(server, db, account):
    # A task that never started, and can't anymore: it fails, and its credit comes back.
    await account.set_credits(0)
    task_id = await insert_task(db, account)
    await age_task(db, task_id, OVERDUE)
    _, data = await export(server, account)
    [task] = data["tasks"]
    assert (task["task_id"], task["status"], task["result"]["error"]) == (task_id, "FAILURE", "TaskLost")
    assert [(m["reason"], m["amount"], m["balance"]) for m in data["credit_history"]] == [("refund", 1, 1)]
    assert data["account"]["credits"] == 1


async def test_the_export_takes_the_session_only(anon, account):
    # Without one, the link goes to the login page, even with the API key: a leaked one can't
    # get the account's profile, nor its webhook's URL, which may have a token in it.
    r = await anon.get("/user/export", headers={"X-API-Key": account.api_key})
    assert (r.status_code, r.headers["location"]) == (307, "/")
    # Nor is it in /docs.
    assert "/user/export" not in (await anon.get("/openapi.json")).json()["paths"]


async def test_the_panel_links_to_the_export(user):
    assert 'href="/user/export"' in (await user.get("/userpanel")).text
