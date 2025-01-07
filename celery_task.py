#!/usr/bin/env python3

from celery import Celery
from shared import CONFIG

from tortoise import Tortoise
from web.db_models import Account
from shared import DB_URL

import time


# Initialize Tortoise ORM and Celery
celery_app = Celery(
    __name__,
    broker=CONFIG["celery"]["broker"],
    backend=CONFIG["celery"]["backend"],
    broker_connection_retry_on_startup=True,
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
    import asyncio
    # Capture the result of the async function
    result = asyncio.run(run_divide(self, x, y, user_id))
    return result  # Return result properly to Celery


async def run_divide(self, x, y, user_id):
    await init_db()

    try:
        # Execute the Node.js script
        time.sleep(5)  # Simulate a long-running operation
        result: float = x / y
    except Exception as e:
        await Tortoise.close_connections()
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e),
        }

    #
    #  --> Sucessful Scrape:
    #
    # Fetch the user account and deduct credits asynchronously
    try:
        account = await Account.get(google_id=user_id)
        if account.credits < 1:
            return {"RESULT": "Not enough credits"}

        # Deduct the credit
        account.credits -= 1
        await account.save()
        print(f"Credit deducted. Remaining credits: {account.credits}")
    except Exception as e:
        await Tortoise.close_connections()
        return {"RESULT": f"Database error: {str(e)}"}

    await Tortoise.close_connections()

    return result
