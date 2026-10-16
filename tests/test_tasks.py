"""Tasks: credits, the API key routes and the task details."""
import asyncio
import re
import time
import tomllib

import pytest
from taskiq import TaskiqResult
from tortoise import Tortoise

from tests.helpers import client_for, insert_task, wait_for_row, wait_until_done

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("worker")]


async def test_api_key_task_charges_one_credit_and_returns_the_result(account, user, anon):
    r = await anon.get("/api/task/divide/10/4", params={"api_key": account.api_key})
    assert r.status_code == 200
    data = await wait_until_done(user, r.json()["id"])
    assert data["result"] == 2.5
    assert await account.credits() == 4


async def test_failed_task_gives_the_credit_back(account, user, anon):
    r = await anon.get("/api/task/divide/1/0", params={"api_key": account.api_key})
    assert r.status_code == 200
    data = await wait_until_done(user, r.json()["id"])
    # The message of a TaskError is for the user, so it's saved.
    assert (data["status"], data["result"]) == (
        "FAILURE", {"status": "error", "message": "Can't divide by zero.", "error": "TaskError"}
    )
    assert await account.credits() == 5


async def test_task_details_page_fetches_its_data(db, account, user):
    task_id = await insert_task(db, account, status="SUCCESS", result=2.0)
    page = await user.get(f"/task_details/{task_id}")
    assert page.status_code == 200
    url = re.search(r"fetch\(`([^`]+)`\)", page.text).group(1).replace("${taskId}", task_id)
    r = await user.get(url)
    assert r.status_code == 200
    assert r.json()["result"] == 2.0


async def test_start_task_queues_any_task(app_module, db, account, user, taskiq_client):
    from tests import worker_app

    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        row = await app_module.Account.get(google_id=account.google_id)
        started = await app_module.start_task(row, worker_app.square, {"n": 3})
    finally:
        await Tortoise.close_connections()

    assert await wait_for_row(db, started.id, "SUCCESS") == ("SUCCESS", 9)
    assert await account.credits() == 4
    history = (await user.get("/user/task_history")).json()
    assert (history[0]["task_type"], history[0]["parameters"]) == ("square", {"n": 3})


async def test_the_panel_lists_the_tasks_with_their_parameters(user):
    r = await user.get("/user/tasks")
    assert r.json() == [{
        "name": "divide",
        "description": "Divide x by y. It waits 5 seconds first, like a long-running task.",
        "parameters": [
            {"name": "x", "type": "integer", "default": None},
            {"name": "y", "type": "integer", "default": None},
        ],
    }]


async def test_the_panel_gets_the_input_type_of_each_parameter(app_module):
    from tests import worker_app  # noqa: F401 (its tasks join the API's broker)

    tasks = {task["name"]: task for task in await app_module.list_tasks(user=None)}
    parameters = [(p["name"], p["type"], p["default"]) for p in tasks["every_kind"]["parameters"]]
    assert parameters == [
        ("flag", "boolean", None),
        ("numbers", "array", None),
        ("options", "object", None),
        # A model's schema is in $defs.
        ("point", "object", None),
        # A union of several types has no input of its own.
        ("either", "string", None),
        # An optional parameter gets the input of its type.
        ("limit", "integer", None),
        ("ratio", "number", 0.5),
    ]


async def test_the_docs_show_each_task_with_its_parameters(anon):
    schema = (await anon.get("/openapi.json")).json()
    operation = schema["paths"]["/api/task/divide"]["post"]
    assert (operation["summary"], operation["description"], operation["tags"]) == (
        "Start divide", "Divide x by y. It waits 5 seconds first, like a long-running task.", ["tasks"]
    )
    parameters = schema["components"]["schemas"]["divide_parameters"]
    assert parameters["properties"] == {"x": {"type": "integer", "title": "X"}, "y": {"type": "integer", "title": "Y"}}
    assert parameters["required"] == ["x", "y"]
    # And the Idempotency-Key header, to retry a request safely.
    assert [(p["name"], p["in"], p["required"]) for p in operation["parameters"]] == [
        ("Idempotency-Key", "header", False)
    ]
    # The panel's routes stay out of them.
    assert "/task/divide" not in schema["paths"]


async def test_any_task_starts_with_its_parameters_in_json(account, user, anon):
    r = await user.post("/task/divide", json={"x": 10, "y": 4})
    assert r.status_code == 200
    assert (await wait_until_done(user, r.json()["id"]))["result"] == 2.5

    r = await anon.post("/api/task/divide", json={"x": "9", "y": 3}, headers={"X-API-Key": account.api_key})
    assert r.status_code == 200
    data = await wait_until_done(user, r.json()["id"])
    assert (data["parameters"], data["result"]) == ({"x": 9, "y": 3}, 3.0)
    assert await account.credits() == 3


async def test_wrong_tasks_or_parameters_spend_no_credit(db, account, user, anon):
    for parameters, field in [
        ({"x": "ten", "y": 4}, "x"),
        ({"x": 1}, "y"),
        # user_id comes from the session: nobody can start tasks for another user.
        ({"x": 1, "y": 2, "user_id": "someone-else"}, "user_id"),
    ]:
        r = await user.post("/task/divide", json=parameters)
        assert (r.status_code, r.json()["detail"][0]["loc"]) == (422, ["body", field])

    assert (await user.post("/task/no-such-task", json={})).status_code == 404
    r = await anon.post("/api/task/divide", json={"x": 1, "y": 2}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    assert await account.credits() == 5
    assert await db.fetchval("SELECT count(*) FROM taskhistory WHERE user_id = $1", account.id) == 0


async def test_the_task_history_shows_the_latest_tasks_up_to_date(
    app_module, db, account, user, taskiq_client
):
    limit = app_module.TASK_HISTORY_LIMIT
    task_ids = [await insert_task(db, account, status="SUCCESS", result=2.0) for _ in range(limit + 4)]
    # The newest one ended, but the worker couldn't save it.
    task_ids.append(await insert_task(db, account))
    await taskiq_client.result_backend.set_result(
        task_ids[-1], TaskiqResult(is_err=False, return_value=3.0, execution_time=0)
    )

    history = (await user.get("/user/task_history")).json()
    assert [task["task_id"] for task in history] == task_ids[::-1][:limit]
    assert (history[0]["status"], history[0]["result"]) == ("SUCCESS", 3.0)


async def test_api_task_details_keeps_401_and_404(db, account, anon):
    task_id = await insert_task(db, account, status="SUCCESS", result=2.0)
    r = await anon.get(f"/api/task_details/{task_id}", params={"api_key": "wrong"})
    assert r.status_code == 401
    r = await anon.get("/api/task_details/no-such-task", params={"api_key": account.api_key})
    assert r.status_code == 404


@pytest.mark.parametrize("route", ["/task/status/", "/task_details/", "/api/task_details_from_user/"])
async def test_unknown_task_gives_404(user, route):
    assert (await user.get(route + "no-such-task")).status_code == 404


async def test_creating_a_task_with_the_cookie_needs_post(account, user):
    # A GET that spends credits could be triggered by a link from another site.
    assert (await user.get("/task/divide/1/1")).status_code == 405
    assert (await user.get("/task/divide")).status_code == 405
    assert await account.credits() == 5


async def test_concurrent_requests_cannot_spend_more_credits_than_the_account_has(account, user):
    await account.set_credits(2)
    rs = await asyncio.gather(*(user.post("/task/divide/6/3") for _ in range(10)))
    accepted = [r.json()["id"] for r in rs if r.status_code == 200]
    assert len(accepted) == 2
    assert sum(r.status_code == 403 for r in rs) == 8

    done = await asyncio.gather(*(wait_until_done(user, task_id) for task_id in accepted))
    assert [d["result"] for d in done] == [2.0, 2.0]
    assert await account.credits() == 0


async def test_regenerating_the_api_key_does_not_undo_credit_changes(account, user):
    await account.set_credits(20)
    stop = asyncio.Event()

    async def regenerate_api_key():
        while not stop.is_set():
            await user.post("/user/api_key")

    loops = [asyncio.create_task(regenerate_api_key()) for _ in range(3)]
    rs = await asyncio.gather(
        *(user.post("/task/divide/6/3") for _ in range(10)),
        *(user.post("/task/divide/6/0") for _ in range(5)),
    )
    assert all(r.status_code == 200 for r in rs)
    done = await asyncio.gather(*(wait_until_done(user, r.json()["id"]) for r in rs))
    stop.set()
    await asyncio.gather(*loops)

    # The divisions by zero fail, and only the 10 real results are charged.
    assert [d["status"] for d in done] == ["SUCCESS"] * 10 + ["FAILURE"] * 5
    assert sum(d["result"] == 2.0 for d in done) == 10
    assert await account.credits() == 10


async def test_with_the_broker_down_the_credit_comes_back_and_no_row_is_left(
    broker_down_server, db, account
):
    async with client_for(broker_down_server, account.cookie) as client:
        start = time.monotonic()
        r = await client.post("/task/divide/1/1")
        elapsed = time.monotonic() - start
    assert r.status_code == 500
    # Redis refuses the connection, so it fails right away.
    assert elapsed < 10
    assert await account.credits() == 5
    assert await db.fetchval("SELECT count(*) FROM taskhistory WHERE user_id = $1", account.id) == 0


async def test_with_the_broker_down_the_errors_keep_its_address_in_the_server_log(
    broker_down_server, db, account
):
    broker = tomllib.loads(broker_down_server.config.read_text())["taskiq"]["broker"]
    address = broker.removeprefix("redis://").removesuffix("/0")
    task_id = await insert_task(db, account)

    async with client_for(broker_down_server, account.cookie) as user:
        rs = [
            await user.post("/task/divide/1/1"),
            await user.get(f"/task/status/{task_id}"),
            await user.get(f"/api/task_details_from_user/{task_id}"),
            await user.get(f"/api/task_details/{task_id}", headers={"X-API-Key": account.api_key}),
        ]
    assert all(r.status_code == 500 for r in rs)
    assert [r.json() for r in rs] == [
        {"detail": "Task initiation failed"},
        {"detail": "Error retrieving task status"},
        {"detail": "Error retrieving task details"},
        {"detail": "Error retrieving task details"},
    ]
    assert address in (broker_down_server.config.parent / "uvicorn.log").read_text()
