"""
The task history, a page at a time, the latest tasks first: the panel's,
with the session, and the API's, with the API key.
"""
import datetime

import pytest
from taskiq import TaskiqResult

from tests.helpers import create_account, insert_task

pytestmark = pytest.mark.anyio

NOT_FOUND = "Task not found or you do not have permission to access it"


@pytest.fixture(params=["session", "api_key"])
def history(request, account, user, anon):
    """Get a page of the account's history, from the panel's route or from the API's."""
    async def get(**params):
        params = {name: value for name, value in params.items() if value is not None}
        if request.param == "session":
            return await user.get("/user/task_history", params=params)
        return await anon.get("/api/task_history", params=params, headers={"X-API-Key": account.api_key})
    return get


def task_ids(page):
    return [task["task_id"] for task in page["tasks"]]


async def test_the_pages_show_the_tasks_up_to_date(app_module, db, account, history, taskiq_client):
    limit = app_module.TASK_HISTORY_LIMIT
    # The oldest and the newest ended, but the worker couldn't save them.
    created = [await insert_task(db, account)]
    created += [await insert_task(db, account, status="SUCCESS", result=2.0) for _ in range(limit + 2)]
    created.append(await insert_task(db, account))
    for task_id in (created[0], created[-1]):
        await taskiq_client.result_backend.set_result(
            task_id, TaskiqResult(is_err=False, return_value=3.0, execution_time=0)
        )
    latest_first = created[::-1]

    first = (await history()).json()
    assert (task_ids(first), first["has_more"]) == (latest_first[:limit], True)
    assert (first["tasks"][0]["status"], first["tasks"][0]["result"]) == ("SUCCESS", 3.0)
    task = first["tasks"][1]
    assert task == {
        "task_id": latest_first[1], "task_type": "divide", "parameters": {"x": 6, "y": 3},
        "status": "SUCCESS", "result": 2.0, "created_at": task["created_at"], "started": False, "cost": 1,
    }
    # With its time zone: without one, the panel would take it for the browser's.
    assert datetime.datetime.fromisoformat(task["created_at"]).utcoffset() == datetime.timedelta(0)

    second = (await history(before=first["tasks"][-1]["task_id"])).json()
    assert (task_ids(second), second["has_more"]) == (latest_first[limit:], False)
    assert (second["tasks"][-1]["status"], second["tasks"][-1]["result"]) == ("SUCCESS", 3.0)


async def test_the_pages_have_every_task_once(db, account, history):
    created = [await insert_task(db, account) for _ in range(6)]
    # Another account's: in no page.
    await insert_task(db, await create_account(db))
    # The first four at the same time, and the last two at the same time, before them.
    now = datetime.datetime.now(datetime.timezone.utc)
    for tasks, age in [(created[:4], 1), (created[4:], 2)]:
        await db.execute(
            "UPDATE taskhistory SET created_at = $2 WHERE task_id = ANY($1::text[])",
            tasks, now - datetime.timedelta(hours=age),
        )
    # The latest first, and of the ones created at the same time, the last one saved.
    latest_first = [*created[3::-1], created[5], created[4]]

    pages, before = [], None
    # At most a page per task: pages that don't move on fail the test instead of hanging it.
    for _ in latest_first:
        page = (await history(before=before, limit=3)).json()
        pages.append(task_ids(page))
        if len(pages) == 1:
            # Started after the first page: it's in the first one, and the next ones don't move.
            newer = await insert_task(db, account)
        if not page["has_more"]:
            break
        before = pages[-1][-1]

    # The last page is full, with no more tasks after it.
    assert pages == [latest_first[:3], latest_first[3:]]
    assert task_ids((await history(limit=3)).json()) == [newer, *latest_first[:2]]


async def test_a_page_comes_after_a_task_of_the_account(db, history):
    another_accounts = await insert_task(db, await create_account(db))
    for before in (another_accounts, "no-such-task"):
        r = await history(before=before)
        assert (r.status_code, r.json()) == (404, {"detail": NOT_FOUND})


async def test_a_page_has_up_to_100_tasks(app_module, history):
    assert app_module.TASK_HISTORY_MAX_LIMIT == 100
    for limit, status in [(0, 422), (101, 422), (1, 200), (100, 200)]:
        assert (await history(limit=limit)).status_code == status, limit
    # The account has no tasks yet.
    assert (await history()).json() == {"tasks": [], "has_more": False}


async def test_the_api_history_needs_an_api_key(anon):
    for headers in ({}, {"X-API-Key": "wrong"}):
        r = await anon.get("/api/task_history", headers=headers)
        assert (r.status_code, r.json()) == (401, {"detail": "Invalid API key"})
