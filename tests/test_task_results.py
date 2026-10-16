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
from redis.asyncio import Redis
from taskiq import TaskiqResult
from taskiq.kicker import AsyncKicker
from taskiq_redis.exceptions import ResultIsMissingError
from tortoise import Tortoise

from tests.helpers import insert_task, task_row, wait_for_row
from web.db_models import TaskError

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("worker")]

ERROR = {"status": "error", "message": "The task failed"}
UID = uuid.UUID("12345678-1234-5678-1234-567812345678")


class ErrorTheApiDoesNotKnow(Exception):
    """Defined only here: the API rebuilds it from its name."""


def failed(error):
    return TaskiqResult(is_err=True, return_value=None, execution_time=0, error=error)


async def test_the_worker_saves_the_result_even_if_taskiq_forgets_it(db, account, user, taskiq_client):
    r = await user.post("/task/divide/6/3")
    assert r.status_code == 200
    task_id = r.json()["id"]

    # Nobody asks the API about it: the worker saves it when it ends.
    assert await wait_for_row(db, task_id, "SUCCESS") == ("SUCCESS", 2.0)

    # As if a day went by: TaskIQ's result backend forgets results after a day.
    backend = taskiq_client.result_backend
    async with Redis(connection_pool=backend.redis_pool) as redis:
        assert await redis.delete(f"{backend.prefix_str}:{task_id}") == 1
    r = await user.get(f"/task/status/{task_id}")
    assert r.json()["status"] == "SUCCESS"
    r = await user.get(f"/api/task_details_from_user/{task_id}")
    assert (r.json()["status"], r.json()["result"]) == ("SUCCESS", 2.0)
    assert await account.credits() == 4


async def test_the_worker_saves_a_failed_task_and_gives_its_credit_back_once(
    db, account, user, anon, taskiq_client
):
    # Like start_task(): reserve the credit and create the row first.
    await account.set_credits(4)
    task_id = await insert_task(db, account)
    await AsyncKicker(task_name="explode", broker=taskiq_client, labels={}).with_task_id(task_id).kiq()

    # Only the exception's type is saved: its message may expose server details.
    failure = ("FAILURE", {**ERROR, "error": "RuntimeError"})
    assert await wait_for_row(db, task_id, "FAILURE") == failure
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
    db, account, user, anon, taskiq_client
):
    # The worker couldn't reach the database: the failures are only in TaskIQ.
    await account.set_credits(1)
    task_ids = [await insert_task(db, account) for _ in range(4)]
    for task_id in task_ids:
        await taskiq_client.result_backend.set_result(
            task_id, failed(ConnectionRefusedError("database down"))
        )

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
    # TaskIQ's view of a task that fails right after the API first reads it.
    reads = itertools.chain([None], itertools.repeat(failed(ZeroDivisionError("division by zero"))))

    async def get_result(task_id):
        result = next(reads)
        if result is None:
            raise ResultIsMissingError
        return result

    monkeypatch.setattr(app_module.broker.result_backend, "get_result", get_result)
    await account.set_credits(4)
    task_id = await insert_task(db, account)

    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        for _ in range(3):
            await app_module.refresh_task(await app_module.TaskHistory.get(task_id=task_id))
    finally:
        await Tortoise.close_connections()

    assert await task_row(db, task_id) == ("FAILURE", {**ERROR, "error": "ZeroDivisionError"})
    assert await account.credits() == 5


@pytest.mark.parametrize(
    ("result", "saved"),
    [
        # TaskIQ's result backend keeps these as JSON.
        (datetime.datetime(2026, 10, 12, 12, 30), ("SUCCESS", "2026-10-12T12:30:00")),
        ({"amount": decimal.Decimal("1.50"), "id": UID}, ("SUCCESS", {"amount": "1.50", "id": str(UID)})),
        # The JSON field takes any str as JSON that is already encoded.
        ("hello", ("SUCCESS", "hello")),
        ("42", ("SUCCESS", "42")),
        (failed(ZeroDivisionError("division by zero")), ("FAILURE", {**ERROR, "error": "ZeroDivisionError"})),
        (failed(ErrorTheApiDoesNotKnow("details")), ("FAILURE", {**ERROR, "error": "ErrorTheApiDoesNotKnow"})),
        # Only a TaskError keeps its message: it's for the user.
        (failed(TaskError("Can't divide by zero.")),
         ("FAILURE", {"status": "error", "message": "Can't divide by zero.", "error": "TaskError"})),
    ],
    ids=[
        "datetime", "decimal-and-uuid", "text", "text-that-looks-like-json", "failure", "unknown-failure",
        "task-error",
    ],
)
async def test_results_are_saved_as_json(result, saved, db, account, anon, taskiq_client):
    task_id = await insert_task(db, account)
    if not isinstance(result, TaskiqResult):
        result = TaskiqResult(is_err=False, return_value=result, execution_time=0)
    await taskiq_client.result_backend.set_result(task_id, result)

    r = await anon.get(f"/api/task_details/{task_id}", params={"api_key": account.api_key})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["result"]) == saved
    assert await task_row(db, task_id) == saved
