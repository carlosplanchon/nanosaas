"""
TaskIQ broker for the tests' worker: the app's tasks, plus one that always
raises, to test what happens to a task that fails with an exception, and
one more to start through start_task().
"""
from taskiq_task import broker


@broker.task(task_name="explode")
async def explode():
    raise RuntimeError("boom, with internal details: 10.0.0.5")


@broker.task(task_name="square")
async def square(n: int, user_id: str):
    return n * n
