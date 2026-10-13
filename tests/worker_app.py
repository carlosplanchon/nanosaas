"""
Celery app for the tests' worker: the app's tasks, plus one that always
raises, to test what happens to a task that fails with an exception.
"""
from celery_task import celery_app


@celery_app.task
def explode():
    raise RuntimeError("boom, with internal details: 10.0.0.5")
