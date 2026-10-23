"""
Times in JSON: the API, the pages' routes, the webhooks' events and the
account's export write them all alike, as ISO 8601 in UTC, ending in Z: as
FastAPI writes its models, and as the examples of Standard Webhooks.
"""
import datetime
import json
import re

import pytest
from tortoise import Tortoise

from tests.helpers import ADMIN, ADMIN_EMAIL, client_for, insert_task, session_cookie
from web.db_models import json_time

pytestmark = pytest.mark.anyio

# An ISO 8601 time, with its offset: Z, or like +00:00.
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")


def times(value):
    """The times in a JSON value, at any depth."""
    if isinstance(value, dict):
        return [time for item in value.values() for time in times(item)]
    if isinstance(value, list):
        return [time for item in value for time in times(item)]
    return [value] if isinstance(value, str) and TIME.fullmatch(value) else []


async def test_every_time_is_in_utc_ending_in_z(app_module, server, db, account, anon):
    task_id = await insert_task(db, account)
    await db.execute(
        "INSERT INTO creditmovement (account_id, amount, balance, reason, created_at) VALUES ($1, 5, 5, 'opening', now())",
        account.id,
    )
    # Disabled, at an address that isn't public: no worker sends its events anywhere.
    webhook_id = await db.fetchval(
        "INSERT INTO webhook (account_id, url, secret, created_at, failing_since, disabled_at, disabled_reason) "
        "VALUES ($1, 'https://192.0.2.1/hook', $2, now(), now(), now(), 'Gone.') RETURNING id",
        account.id, "whsec_" + "a" * 44,
    )
    # The task ends, as the worker ends it, and its event is recorded.
    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        assert await app_module.TaskHistory.finish(task_id, 2.0)
    finally:
        await Tortoise.close_connections()

    headers = {"X-API-Key": account.api_key}
    documents = {
        "the event": json.loads(await db.fetchval("SELECT payload FROM webhookdelivery WHERE webhook_id = $1", webhook_id)),
        "the task's details": (await anon.get(f"/api/task_details/{task_id}", headers=headers)).json(),
        "the task history": (await anon.get("/api/task_history", headers=headers)).json(),
        "the credit history": (await anon.get("/api/credit_history", headers=headers)).json(),
    }
    async with client_for(server, account.cookie) as user:
        documents["the task's details, for the panel"] = (await user.get(f"/api/task_details_from_user/{task_id}")).json()
        documents["the webhook"] = (await user.get("/user/webhook")).json()
        # When each key was created, and used, as the requests above did.
        documents["the API keys"] = (await user.get("/user/api_keys")).json()
        documents["the export"] = json.loads((await user.get("/user/export")).content)
    await db.execute(
        "INSERT INTO account (google_id, email, picture, provider, created_at, credits) "
        "VALUES ($1, $2, '', 'google', now(), 0) ON CONFLICT (google_id) DO NOTHING",
        ADMIN, ADMIN_EMAIL,
    )
    async with client_for(server, session_cookie(ADMIN)) as admin:
        documents["the admin's accounts"] = (await admin.get("/admin/accounts")).json()

    for name, document in documents.items():
        found = times(document)
        assert found and all(time.endswith("Z") for time in found), (name, found)
    # The same time, as the same text, wherever it is.
    created_at = documents["the task's details"]["created_at"]
    assert documents["the event"]["data"]["created_at"] == created_at
    assert documents["the task history"]["tasks"][0]["created_at"] == created_at
    assert documents["the export"]["tasks"][0]["created_at"] == created_at


def test_a_time_in_another_zone_is_written_in_utc():
    # Like 23:30 in Uruguay, which is 02:30 of the next day in UTC.
    uruguay = datetime.timezone(datetime.timedelta(hours=-3))
    assert json_time(datetime.datetime(2026, 10, 22, 23, 30, 0, 123456, tzinfo=uruguay)) == "2026-10-23T02:30:00.123456Z"
    assert json_time(datetime.datetime(2026, 10, 23, 2, 30, tzinfo=datetime.timezone.utc)) == "2026-10-23T02:30:00Z"
    assert json_time(None) is None
