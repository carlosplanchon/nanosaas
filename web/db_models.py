#!/usr/bin/env python3

import hashlib
import json

from pydantic_core import to_jsonable_python

from tortoise.models import Model
from tortoise import fields, timezone
from tortoise.expressions import F
from tortoise.transactions import in_transaction


# Define Account Model using TortoiseORM
class Account(Model):
    id = fields.IntField(primary_key=True)
    google_id = fields.CharField(max_length=255, unique=True)
    email = fields.CharField(max_length=255, unique=True)
    first_name = fields.CharField(max_length=50, null=True)
    last_name = fields.CharField(max_length=50, null=True)
    display_name = fields.CharField(max_length=100, null=True)
    picture = fields.CharField(max_length=255)
    provider = fields.CharField(max_length=50)
    created_at = fields.DatetimeField(auto_now_add=True)
    credits = fields.IntField(default=25)  # New accounts get INITIAL_CREDITS, set in config.toml
    # The API key itself isn't stored: it's shown once, when generated. This
    # keeps "first 8 characters$SHA-256" of it, in the api_key column. The
    # prefix tells keys apart, and the unique index finds the account.
    api_key_hash = fields.CharField(
        max_length=255, null=True, unique=True, source_field="api_key"
    )

    def __str__(self):
        return self.display_name

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """The stored form of an API key (see api_key_hash)."""
        return f"{api_key[:8]}${hashlib.sha256(api_key.encode()).hexdigest()}"

    @classmethod
    async def get_by_api_key(cls, api_key: str):
        return await cls.get_or_none(api_key_hash=cls.hash_api_key(api_key))

    @property
    def api_key_prefix(self) -> str | None:
        """The start of the current API key, to tell which one it is."""
        return self.api_key_hash[:8] if self.api_key_hash else None

    @classmethod
    async def reserve_credit(cls, google_id: str) -> bool:
        """
        Take one credit in a single UPDATE, so concurrent requests
        can't spend more credits than the account has.
        Returns False if there were no credits left.
        """
        updated = await cls.filter(
            google_id=google_id,
            credits__gte=1
        ).update(credits=F("credits") - 1)
        return updated == 1

    @classmethod
    async def refund_credit(cls, google_id: str) -> None:
        """Give back a credit taken with reserve_credit()."""
        await cls.filter(google_id=google_id).update(
            credits=F("credits") + 1
        )


class TaskError(Exception):
    """
    Raised by a task to fail with a message for the user: it's saved
    with the task, while other exceptions only keep their type.
    """


class TaskHistory(Model):
    id = fields.IntField(primary_key=True)
    user = fields.ForeignKeyField(
        "models.Account",
        related_name="task_history"
    )
    task_id = fields.CharField(max_length=255, unique=True)
    task_type = fields.CharField(max_length=50)
    parameters = fields.JSONField()
    # PENDING until the task ends as SUCCESS or FAILURE.
    status = fields.CharField(max_length=50, default="PENDING")
    result = fields.JSONField(null=True)  # Store task results or error details
    created_at = fields.DatetimeField(auto_now_add=True)
    # When a worker took the task from the queue (see start()).
    started_at = fields.DatetimeField(null=True)
    # The Idempotency-Key of the request that started the task, if it had
    # one: the account's requests with the same key get this task.
    idempotency_key = fields.CharField(max_length=255, null=True)

    class Meta:
        unique_together = (("user", "idempotency_key"),)

    def __str__(self):
        return f"{self.task_type} - {self.task_id}"

    @classmethod
    async def start(cls, task_id: str) -> bool:
        """
        Mark a pending task as started, only the first time.

        The worker calls this before it runs a task. Redis can queue a
        task twice, when it runs the LPUSH but the reply is lost and
        redis-py sends it again, or queue a task the API gave up for the
        same reason (see give_up()). Only the first copy of a pending
        task starts, so it runs once.
        Returns False if the task has no pending row or already started.
        """
        updated = await cls.filter(
            task_id=task_id, status="PENDING", started_at__isnull=True
        ).update(started_at=timezone.now())
        return updated == 1

    @classmethod
    async def give_up(cls, task_id: str, google_id: str) -> bool:
        """
        Delete a task that couldn't be queued and give its credit back,
        unless a worker already started it: then Redis did queue it, and
        only the reply to the API was lost.
        Returns False if a worker started it.
        """
        async with in_transaction():
            deleted = await cls.filter(
                task_id=task_id, status="PENDING", started_at__isnull=True
            ).delete()
            if deleted:
                await Account.refund_credit(google_id)

        return deleted == 1

    @classmethod
    async def finish(cls, task_id: str, result=None, error: BaseException | None = None) -> bool:
        """
        Save a pending task's result, or the error it failed with, only
        the first time.

        The worker calls this when a task ends, and the API when it finds
        a finished task the worker couldn't save. The update only matches
        pending rows, so a failed task's credit is given back once.
        Returns False if the task has no row or was already finished.
        """
        if error is not None:
            # Keep only the exception's type: its message may expose
            # details of the servers. A TaskError's is for the user.
            result = {
                "status": "error",
                "message": str(error) if isinstance(error, TaskError) else "The task failed",
                "error": type(error).__name__,
            }

        # Encode it here: the JSON field can't store datetimes, Decimals or
        # UUIDs, and it takes any str as JSON that is already encoded.
        encoded = json.dumps(to_jsonable_python(result, fallback=str))
        status = "SUCCESS" if error is None else "FAILURE"

        async with in_transaction():
            updated = await cls.filter(task_id=task_id, status="PENDING").update(
                status=status, result=encoded
            )

            if updated and error is not None:
                # Only successful tasks are charged.
                task = await cls.get(task_id=task_id).prefetch_related("user")
                await Account.refund_credit(task.user.google_id)

        return updated == 1
