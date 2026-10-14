"""
Saving task results: the worker saves each task when it ends, the API saves
the ones the worker couldn't, and a failed task's credit comes back once.
"""
import asyncio
import datetime
import decimal
import itertools
import uuid

import pytest
from celery import states
from tortoise import Tortoise

from tests.helpers import insert_task, task_row, wait_for_row

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("worker")]

ERROR = {"status": "error", "message": "The task failed"}
UID = uuid.UUID("12345678-1234-5678-1234-567812345678")


async def test_the_worker_saves_the_result_even_if_celery_forgets_it(db, account, user, celery_client):
    r = await user.post("/task/divide/6/3")
    assert r.status_code == 200
    task_id = r.json()["id"]

    # Nobody asks the API about it: the worker saves it when it ends.
    assert await wait_for_row(db, task_id, "SUCCESS") == ("SUCCESS", 2.0)

    # As if a day went by: Celery forgets results after result_expires.
    celery_client.AsyncResult(task_id).forget()
    r = await user.get(f"/task/status/{task_id}")
    assert r.json()["status"] == "SUCCESS"
    r = await user.get(f"/api/task_details_from_user/{task_id}")
    assert (r.json()["status"], r.json()["result"]) == ("SUCCESS", 2.0)
    assert await account.credits() == 4


async def test_the_worker_saves_a_failed_task_and_gives_its_credit_back_once(
    db, account, user, anon, celery_client
):
    # Like start_divide_task(): reserve the credit and create the row first.
    await account.set_credits(4)
    task_id = await insert_task(db, account)
    celery_client.send_task("tests.worker_app.explode", task_id=task_id)

    # Only the exception's type is saved: its message may expose server details.
    failed = ("FAILURE", {**ERROR, "error": "RuntimeError"})
    assert await wait_for_row(db, task_id, "FAILURE") == failed
    assert await account.credits() == 5

    # The API finds it already finished: no second refund.
    rs = await asyncio.gather(
        *(user.get(f"/task/status/{task_id}") for _ in range(5)),
        *(anon.get(f"/api/task_details/{task_id}", params={"api_key": account.api_key})
          for _ in range(5)),
        user.get("/user/task_history"),
    )
    assert all(r.status_code == 200 for r in rs)
    assert await account.credits() == 5


async def test_the_api_saves_failures_the_worker_could_not_and_refunds_them_once(
    db, account, user, anon, celery_client
):
    # The worker couldn't reach the database: the failures are only in Celery.
    await account.set_credits(1)
    task_ids = [await insert_task(db, account) for _ in range(4)]
    for task_id in task_ids:
        celery_client.backend.mark_as_failure(task_id, ConnectionRefusedError("database down"))

    # Every route at once, three times per task: only the first request that
    # saves each failure gives its credit back.
    requests = [user.get("/user/task_history") for _ in range(4)]
    for task_id in task_ids:
        for _ in range(3):
            requests += [
                user.get(f"/task/status/{task_id}"),
                user.get(f"/api/task_details_from_user/{task_id}"),
                anon.get(f"/api/task_details/{task_id}", params={"api_key": account.api_key}),
            ]
    rs = await asyncio.gather(*requests)

    assert all(r.status_code == 200 for r in rs)
    for task_id in task_ids:
        assert await task_row(db, task_id) == ("FAILURE", {**ERROR, "error": "ConnectionRefusedError"})
    assert await account.credits() == 5


async def test_a_task_that_fails_while_the_api_reads_it_gives_its_credit_back(
    app_module, monkeypatch, db, account
):
    # Celery's view of a task that fails right after the API first reads it.
    reads = itertools.chain(["PENDING"], itertools.repeat("FAILURE"))

    class FailsAfterTheFirstRead:
        def __init__(self, task_id):
            pass

        @property
        def state(self):
            return next(reads)

        status = state

        def ready(self):
            return self.state in states.READY_STATES

        result = ZeroDivisionError("division by zero")

    monkeypatch.setattr(app_module, "AsyncResult", FailsAfterTheFirstRead)
    await account.set_credits(4)
    task_id = await insert_task(db, account)

    await Tortoise.init(db_url=app_module.DB_URL, modules={"models": ["web.db_models"]})
    try:
        for _ in range(2):
            await app_module.refresh_task(await app_module.TaskHistory.get(task_id=task_id))
    finally:
        await Tortoise.close_connections()

    assert await task_row(db, task_id) == ("FAILURE", {**ERROR, "error": "ZeroDivisionError"})
    assert await account.credits() == 5


@pytest.mark.parametrize(
    ("state", "result", "saved"),
    [
        # Celery decodes these back into Python objects.
        ("SUCCESS", datetime.datetime(2026, 10, 12, 12, 30), "2026-10-12 12:30:00"),
        ("SUCCESS", {"amount": decimal.Decimal("1.50"), "id": UID}, {"amount": "1.50", "id": str(UID)}),
        # The JSON field takes any str as JSON that is already encoded.
        ("SUCCESS", "hello", "hello"),
        ("SUCCESS", "42", "42"),
        ("REVOKED", None, {**ERROR, "error": "TaskRevokedError"}),
    ],
    ids=["datetime", "decimal-and-uuid", "text", "text-that-looks-like-json", "revoked"],
)
async def test_results_are_saved_as_json(state, result, saved, db, account, anon, celery_client):
    task_id = await insert_task(db, account)
    if state == "REVOKED":
        celery_client.backend.mark_as_revoked(task_id)
    else:
        celery_client.backend.store_result(task_id, result, state)

    r = await anon.get(f"/api/task_details/{task_id}", params={"api_key": account.api_key})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["result"]) == (state, saved)
    assert await task_row(db, task_id) == (state, saved)
