"""
Tasks that don't end on their own: their worker died, or they waited or ran
too long. They fail, giving their credit back, once TaskIQ can't run them.
"""
import asyncio
import datetime
import time

import pytest
from taskiq import TaskiqResult

from tests.helpers import age_task, client_for, insert_task, task_row, wait_for_log, wait_until_done

pytestmark = pytest.mark.anyio

ERROR = {"status": "error", "message": "The task failed"}
# With the default queue_timeout and time_limit, 600 s each, plus a minute.
DEADLINE = datetime.timedelta(seconds=600 + 600 + 60)


async def test_a_task_whose_worker_dies_fails_after_its_deadline(
    short_limits_server, start_worker, db, account
):
    worker = start_worker(short_limits_server)
    async with client_for(short_limits_server, account.cookie) as user:
        r = await user.post("/task/divide/6/3")
        task_id = r.json()["id"]
        await wait_for_log(worker.log, f"Executing task divide with ID: {task_id}")
        await asyncio.sleep(1)
        worker.kill()  # In the middle of the task's 5 seconds.

        # No other worker runs it again, and TaskIQ never hears back from it:
        # it stays PENDING, holding its credit.
        start_worker(short_limits_server)
        await asyncio.sleep(5)
        assert (await user.get(f"/task/status/{task_id}")).json()["status"] == "PENDING"
        assert await account.credits() == 4

        # Past its deadline it fails, and its credit comes back only once.
        await age_task(db, task_id, datetime.timedelta(hours=1))
        rs = await asyncio.gather(
            user.get("/user/task_history"),
            user.get("/user/credits"),
            *(user.get(f"/api/task_details_from_user/{task_id}") for _ in range(5)),
        )
    assert all(r.status_code == 200 for r in rs)
    assert await task_row(db, task_id) == ("FAILURE", {**ERROR, "error": "TaskLost"})
    assert await account.credits() == 5


async def test_a_task_that_waits_too_long_in_the_queue_is_dropped(
    short_limits_server, start_worker, account
):
    async with client_for(short_limits_server, account.cookie) as user:
        # No worker yet: it waits in the queue longer than queue_timeout (2 s).
        r = await user.post("/task/divide/6/3")
        task_id = r.json()["id"]
        await asyncio.sleep(3)

        worker = start_worker(short_limits_server)
        data = await wait_until_done(user, task_id)
    assert (data["status"], data["result"]) == ("FAILURE", {**ERROR, "error": "TaskExpired"})
    assert await account.credits() == 5
    assert f"Task {task_id} waited in the queue longer than queue_timeout: dropped." in worker.log.read_text()


async def test_a_task_that_runs_too_long_is_stopped(short_limits_server, start_worker, account):
    start_worker(short_limits_server)
    async with client_for(short_limits_server, account.cookie) as user:
        # divide takes 5 s: more than time_limit (4 s).
        r = await user.post("/task/divide/6/3")
        data = await wait_until_done(user, r.json()["id"])
    assert (data["status"], data["result"]) == ("FAILURE", {**ERROR, "error": "TimeoutError"})
    assert await account.credits() == 5


async def test_a_worker_waiting_for_tasks_stays_up(short_limits_server, start_worker):
    # Waiting for tasks longer than redis-py's 5 s socket timeout would kill it.
    worker = start_worker(short_limits_server)
    await asyncio.sleep(7)
    assert "is dead" not in worker.log.read_text()


async def test_a_worker_waiting_for_tasks_stops_when_told(short_limits_server, start_worker):
    worker = start_worker(short_limits_server)
    start = time.monotonic()
    worker.stop()
    # Otherwise it would wait for one more task, and run it.
    assert time.monotonic() - start < 5


async def test_a_task_is_given_up_only_after_its_deadline(db, account, user, taskiq_client):
    # Three tasks hold credits, and TaskIQ knows nothing about two of them.
    await account.set_credits(2)
    almost = await insert_task(db, account)
    await age_task(db, almost, DEADLINE - datetime.timedelta(minutes=1))
    lost = await insert_task(db, account)
    await age_task(db, lost, DEADLINE + datetime.timedelta(minutes=1))
    # The worker couldn't save this one, but TaskIQ has its result.
    done = await insert_task(db, account)
    await age_task(db, done, DEADLINE + datetime.timedelta(minutes=1))
    await taskiq_client.result_backend.set_result(
        done, TaskiqResult(is_err=False, return_value=2.0, execution_time=0)
    )

    assert (await user.get("/user/task_history")).status_code == 200
    assert await task_row(db, almost) == ("PENDING", None)
    assert await task_row(db, lost) == ("FAILURE", {**ERROR, "error": "TaskLost"})
    assert await task_row(db, done) == ("SUCCESS", 2.0)
    assert await account.credits() == 3


@pytest.mark.parametrize("via", ["cookie", "api_key"])
async def test_reading_the_credits_gives_back_those_of_lost_tasks(via, db, account, user, anon):
    # The only credit left is held by a task lost a day ago.
    await account.set_credits(0)
    task_id = await insert_task(db, account)
    await age_task(db, task_id, datetime.timedelta(days=1))

    if via == "cookie":
        r = await user.get("/user/credits")
    else:
        r = await anon.get("/api/user_credits", headers={"X-API-Key": account.api_key})
    assert r.json() == {"credits": 1}
    assert await task_row(db, task_id) == ("FAILURE", {**ERROR, "error": "TaskLost"})


async def test_a_new_task_can_use_the_credit_of_a_lost_one(db, account, user, worker):
    await account.set_credits(0)
    task_id = await insert_task(db, account)
    await age_task(db, task_id, datetime.timedelta(days=1))

    assert (await user.post("/task/divide/6/3")).status_code == 200
    assert (await task_row(db, task_id))[0] == "FAILURE"
