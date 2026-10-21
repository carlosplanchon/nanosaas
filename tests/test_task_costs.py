"""
What tasks cost: 1 credit, or what their cost label says. It's reserved when
they start, and given back if they fail, are canceled or get lost: each task
gives back what it cost when it started, which its history row keeps.
"""
import types

import pytest
from fastapi import HTTPException
from taskiq import TaskiqResult
from tortoise import Tortoise

from tests.helpers import insert_task, wait_for_row

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("worker")]

ERROR = {"status": "error", "message": "It failed, as asked.", "error": "TaskError"}


async def start(app_module, account, task, parameters):
    """
    start_task() in this process: the API only has the app's tasks, and
    "costly", which costs 3 credits, is one of the tests' worker.
    """
    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        row = await app_module.Account.get(google_id=account.google_id)
        return await app_module.start_task(row, task, parameters)
    finally:
        await Tortoise.close_connections()


async def history_row_count(db, account):
    return await db.fetchval("SELECT count(*) FROM taskhistory WHERE user_id = $1", account.id)


async def test_a_task_takes_its_cost_and_keeps_it_if_it_succeeds(app_module, db, account, taskiq_client):
    from tests import worker_app

    started = await start(app_module, account, worker_app.costly, {"fail": False})
    assert await account.credits() == 5 - 3
    assert await wait_for_row(db, started.id, "SUCCESS") == ("SUCCESS", "done")
    assert await account.credits() == 2

    # 2 credits aren't enough for it.
    with pytest.raises(HTTPException) as error:
        await start(app_module, account, worker_app.costly, {"fail": False})
    assert (error.value.status_code, error.value.detail) == (403, "Not enough credits")
    assert (await account.credits(), await history_row_count(db, account)) == (2, 1)


async def test_a_task_that_fails_gives_its_cost_back(app_module, db, account, taskiq_client):
    from tests import worker_app

    started = await start(app_module, account, worker_app.costly, {"fail": True})
    assert await account.credits() == 2
    assert await wait_for_row(db, started.id, "FAILURE") == ("FAILURE", ERROR)
    assert await account.credits() == 5


async def test_each_task_gives_back_what_it_cost_when_it_started(app_module, db, account, user, taskiq_client):
    # Three tasks that cost 4 credits each when they started: 12 of 13.
    await account.set_credits(1)
    canceled, failed, given_up = [await insert_task(db, account, cost=4) for _ in range(3)]

    # Canceled with the panel.
    assert (await user.post(f"/task/cancel/{canceled}")).status_code == 200
    assert await account.credits() == 5

    # Failed, in TaskIQ's result backend only: the history saves it.
    await taskiq_client.result_backend.set_result(
        failed, TaskiqResult(is_err=True, return_value=None, execution_time=0, error=RuntimeError("boom"))
    )
    assert (await user.get("/user/task_history")).status_code == 200
    assert await account.credits() == 9

    # Given up by the API, which couldn't queue it.
    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        assert await app_module.TaskHistory.give_up(given_up, account.google_id)
    finally:
        await Tortoise.close_connections()
    assert (await account.credits(), await history_row_count(db, account)) == (13, 2)


async def test_the_panel_the_docs_and_the_history_show_the_costs(app_module, db, account, user, anon):
    from tests import worker_app  # noqa: F401 (adds its tasks to the broker)

    costs = {task["name"]: task["cost"] for task in await app_module.list_tasks(user=None)}
    assert (costs["divide"], costs["costly"]) == (1, 3)

    docs = (await anon.get("/openapi.json")).json()["paths"]["/api/task/divide"]["post"]["description"]
    assert docs.endswith("\n\nCosts 1 credit, given back if the task fails or is canceled.")

    task_id = await insert_task(db, account, status="SUCCESS", result=2.0, cost=2)
    [task] = (await user.get("/user/task_history")).json()["tasks"]
    details = await anon.get(f"/api/task_details/{task_id}", headers={"X-API-Key": account.api_key})
    assert (task["cost"], details.json()["cost"]) == (2, 2)


def test_a_cost_label_is_a_whole_number_of_credits(app_module):
    assert app_module.task_cost(types.SimpleNamespace(task_name="free", labels={})) == 1
    for cost in (0, -1, 1.5, "3", True, None):
        with pytest.raises(ValueError, match="isn't a whole number of credits, 1 or more"):
            app_module.task_cost(types.SimpleNamespace(task_name="wrong", labels={"cost": cost}))
