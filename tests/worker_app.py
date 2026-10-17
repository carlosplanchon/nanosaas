"""
TaskIQ broker for the tests' worker: the app's tasks, plus one that always
raises, to test what happens to a task that fails with an exception, one
more to start through start_task(), one with a parameter of each kind, for
the panel's inputs, and one that returns the IDs the worker gives it.
"""
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from taskiq_task import broker


@broker.task(task_name="explode")
async def explode():
    raise RuntimeError("boom, with internal details: 10.0.0.5")


@broker.task(task_name="square")
async def square(n: int, user_id: str):
    return n * n


@broker.task(task_name="ids")
async def ids(user_id: str, task_id: str):
    return {"user_id": user_id, "task_id": task_id}


class Point(BaseModel):
    x: int
    y: int


class Color(Enum):
    RED = "red"
    GREEN = "green"


@broker.task(task_name="every_kind")
async def every_kind(
    flag: bool,
    numbers: list[int],
    weights: dict[str, int],
    point: Point,
    either: int | str,
    color: Color,
    limit: int | None = None,
    ratio: float = 0.5,
    size: Literal["s", "m", "l"] = "m",
    level: Literal[1, 2, 3] = 2,
    order: Literal["desc"] | None = None,
    *,
    user_id: str,
):
    """Return its parameters, one of each kind."""
    return {
        "flag": flag, "numbers": numbers, "weights": weights, "point": point.model_dump(), "either": either,
        "color": color.value, "limit": limit, "ratio": ratio, "size": size, "level": level, "order": order,
    }
