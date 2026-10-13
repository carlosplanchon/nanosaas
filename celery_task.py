#!/usr/bin/env python3

from celery import Celery
from celery import states
from celery.signals import task_postrun
from shared import CONFIG

from tortoise import Tortoise
from web.db_models import Account, TaskHistory
from shared import DB_URL

import asyncio
import time


# Initialize Celery
celery_app = Celery(
    __name__,
    broker=CONFIG["celery"]["broker"],
    backend=CONFIG["celery"]["backend"],
    broker_connection_retry_on_startup=True,
    # Give up on the result backend after 2 s. With the default policy, a
    # request that queued a task waited 19 s to fail when Redis was down.
    result_backend_transport_options={"retry_policy": {"timeout": 2.0}},
)


# Initialize Tortoise ORM (No schema generation)
async def init_db():
    await Tortoise.init(
        db_url=DB_URL,
        modules={
            "models": ["web.db_models"]
        }
    )


# Define the Celery task properly handling async code
@celery_app.task(bind=True)
def divide(self, x, y, user_id):
    # Capture the result of the async function
    result = asyncio.run(run_divide(self, x, y, user_id))
    return result  # Return result properly to Celery


async def run_divide(self, x, y, user_id):
    await init_db()

    try:
        time.sleep(5)  # Simulate a long-running operation
        result: float = x / y
    except Exception as e:
        # The credit was reserved when the task was queued, and only
        # successful runs are charged: give it back.
        await Account.refund_credit(user_id)
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e),
        }
    finally:
        await Tortoise.close_connections()

    return result


@task_postrun.connect
def save_finished_task(task_id=None, state=None, retval=None, **kwargs):
    """
    Save every task in its history row as soon as it ends, instead of
    waiting for someone to check it before Celery forgets the result
    (after a day). Failed tasks get their credit back.
    """
    if state in states.READY_STATES:
        asyncio.run(save_task(task_id, state, retval))


async def save_task(task_id, state, retval):
    await init_db()

    try:
        await TaskHistory.finish(task_id, state, retval)
    finally:
        await Tortoise.close_connections()
