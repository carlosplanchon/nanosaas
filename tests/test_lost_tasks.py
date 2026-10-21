"""
Tasks that don't end on their own: their worker died, so they run again, or
they waited or ran too long. They fail, giving their credit back, once
TaskIQ can't run them.
"""
import asyncio
import datetime
import time
import tomllib

import pytest
from taskiq import TaskiqResult
from taskiq.serializers import JSONSerializer
from taskiq_redis import RedisAsyncResultBackend

from tests.helpers import (
    age_task, client_for, create_account, insert_task, task_row, wait_for_log, wait_for_row, wait_until_done
)

pytestmark = pytest.mark.anyio

ERROR = {"status": "error", "message": "The task failed"}
# With the default queue_timeout, time_limit, lease and attempts: 3 runs of
# 600 + 600 + 2 * 30 seconds, plus a minute.
DEADLINE = datetime.timedelta(seconds=3 * (600 + 600 + 2 * 30) + 60)
# For a task that never started: the default queue_timeout, plus a minute.
QUEUE_DEADLINE = datetime.timedelta(seconds=600 + 60)
MINUTE = datetime.timedelta(minutes=1)


async def mark_started(db, task_id):
    """As if a worker started the task, with a lease for a day: no worker queues it again."""
    await db.execute(
        "UPDATE taskhistory SET started_at = created_at, attempts = 1, lease_until = now() + interval '1 day' "
        "WHERE task_id = $1",
        task_id,
    )


@pytest.fixture
async def retry_account(retry_db):
    """An account in the database of retry_server."""
    return await create_account(retry_db)


async def attempts(db, task_id):
    return await db.fetchval("SELECT attempts FROM taskhistory WHERE task_id = $1", task_id)


async def start_divide(server, account):
    async with client_for(server, account.cookie) as user:
        return (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]


async def kill_while_running(worker, task_id):
    await wait_for_log(worker.log, f"Executing task divide with ID: {task_id}")
    await asyncio.sleep(1)
    worker.kill()  # In the middle of the task's 5 seconds.


async def test_a_task_whose_worker_dies_runs_again_in_another_one(
    retry_server, start_worker, retry_db, retry_account
):
    worker = start_worker(retry_server)
    task_id = await start_divide(retry_server, retry_account)
    await kill_while_running(worker, task_id)

    # Its lease expires 2 seconds after its last renewal, and the other worker queues it again.
    other = start_worker(retry_server)
    assert await wait_for_row(retry_db, task_id, "SUCCESS") == ("SUCCESS", 2.0)
    assert f"Task {task_id} lost its worker: queued again, after 1 of 2 runs." in other.log.read_text()
    assert await attempts(retry_db, task_id) == 2
    assert await retry_account.credits() == 4


async def test_a_task_whose_workers_keep_dying_fails_after_its_attempts(
    retry_server, start_worker, retry_db, retry_account
):
    workers = [start_worker(retry_server)]
    task_id = await start_divide(retry_server, retry_account)
    for _ in range(2):
        await kill_while_running(workers[-1], task_id)
        workers.append(start_worker(retry_server))

    assert await wait_for_row(retry_db, task_id, "FAILURE") == ("FAILURE", {**ERROR, "error": "TaskLost"})
    # The worker writes it right after it saves the failure.
    await wait_for_log(workers[-1].log, f"Task {task_id} lost its worker in each of its 2 runs: failed.")
    assert await retry_account.credits() == 5


async def test_a_task_that_runs_longer_than_its_lease_runs_once(
    retry_server, start_worker, retry_db, retry_account
):
    # Its worker renews its lease while it runs its 5 seconds, so neither worker queues it again.
    workers = [start_worker(retry_server), start_worker(retry_server)]
    task_id = await start_divide(retry_server, retry_account)
    assert await wait_for_row(retry_db, task_id, "SUCCESS") == ("SUCCESS", 2.0)
    assert await attempts(retry_db, task_id) == 1
    log = "".join(worker.log.read_text() for worker in workers)
    assert log.count(f"Executing task divide with ID: {task_id}") == 1
    assert "queued again" not in log


async def test_a_task_that_ended_unsaved_is_saved_instead_of_run_again(
    retry_server, start_worker, retry_db, retry_account
):
    # Its worker ended it, couldn't save it, and died: its lease expired, but TaskIQ has its result.
    task_id = await insert_task(retry_db, retry_account)
    await retry_db.execute(
        "UPDATE taskhistory SET started_at = now(), lease_until = now(), attempts = 1 WHERE task_id = $1",
        task_id,
    )
    backend = tomllib.loads(retry_server.config.read_text())["taskiq"]["backend"]
    results = RedisAsyncResultBackend(backend, serializer=JSONSerializer(), prefix_str="nanosaas-result")
    await results.set_result(task_id, TaskiqResult(is_err=False, return_value=2.0, execution_time=0))
    await results.shutdown()

    worker = start_worker(retry_server)
    assert await wait_for_row(retry_db, task_id, "SUCCESS") == ("SUCCESS", 2.0)
    assert "Executing task" not in worker.log.read_text()


async def test_a_task_that_waits_too_long_in_the_queue_is_dropped(
    short_limits_server, start_worker, account
):
    async with client_for(short_limits_server, account.cookie) as user:
        # No worker yet: it waits in the queue longer than queue_timeout (2 s).
        r = await user.post("/task/divide", json={"x": 6, "y": 3})
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
        r = await user.post("/task/divide", json={"x": 6, "y": 3})
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
    # Tasks that hold credits, and TaskIQ knows nothing about all but one.
    await account.set_credits(0)
    # One that never started can't anymore once it waited queue_timeout: a worker would drop it.
    waiting = await insert_task(db, account)
    await age_task(db, waiting, QUEUE_DEADLINE - MINUTE)
    never_started = await insert_task(db, account)
    await age_task(db, never_started, QUEUE_DEADLINE + MINUTE)
    # One that started has until each of its runs could have ended.
    running = await insert_task(db, account)
    await age_task(db, running, DEADLINE - MINUTE)
    await mark_started(db, running)
    lost = await insert_task(db, account)
    await age_task(db, lost, DEADLINE + MINUTE)
    await mark_started(db, lost)
    # The worker couldn't save this one, but TaskIQ has its result.
    done = await insert_task(db, account)
    await age_task(db, done, DEADLINE + MINUTE)
    await taskiq_client.result_backend.set_result(
        done, TaskiqResult(is_err=False, return_value=2.0, execution_time=0)
    )

    assert (await user.get("/user/task_history")).status_code == 200
    assert await task_row(db, waiting) == ("PENDING", None)
    assert await task_row(db, never_started) == ("FAILURE", {**ERROR, "error": "TaskLost"})
    assert await task_row(db, running) == ("PENDING", None)
    assert await task_row(db, lost) == ("FAILURE", {**ERROR, "error": "TaskLost"})
    assert await task_row(db, done) == ("SUCCESS", 2.0)
    assert await account.credits() == 2


@pytest.mark.parametrize("via", ["cookie", "api_key"])
async def test_reading_the_credits_gives_back_those_of_lost_tasks(via, db, account, user, anon):
    # The only credit left is held by a task that never started, and can't anymore.
    await account.set_credits(0)
    task_id = await insert_task(db, account)
    await age_task(db, task_id, QUEUE_DEADLINE + MINUTE)

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

    assert (await user.post("/task/divide", json={"x": 6, "y": 3})).status_code == 200
    assert (await task_row(db, task_id))[0] == "FAILURE"
