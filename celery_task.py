#!/usr/bin/env python3

from celery import Celery

import time


# Initialize Tortoise ORM and Celery
celery_app = Celery(
    __name__,
    broker="redis://127.0.0.1:6379/0",
    backend="redis://127.0.0.1:6379/0",
    broker_connection_retry_on_startup=True,
)


@celery_app.task
def divide(x, y, user_id):
    time.sleep(5)  # Simulate a long-running operation
    if y == 0:
        return {"result": "UNDEFINED"}
    return x / y
