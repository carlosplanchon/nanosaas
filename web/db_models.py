#!/usr/bin/env python3

import json

from celery import states

from tortoise.models import Model
from tortoise import fields
from tortoise.expressions import F
from tortoise.transactions import in_transaction


# Define Account Model using TortoiseORM
class Account(Model):
    id = fields.IntField(pk=True)
    google_id = fields.CharField(max_length=255, unique=True)
    email = fields.CharField(max_length=255, unique=True)
    first_name = fields.CharField(max_length=50, null=True)
    last_name = fields.CharField(max_length=50, null=True)
    display_name = fields.CharField(max_length=100, null=True)
    picture = fields.CharField(max_length=255)
    provider = fields.CharField(max_length=50)
    created_at = fields.DatetimeField(auto_now_add=True)
    credits = fields.IntField(default=25)  # Start users with 5 credits
    api_key = fields.CharField(max_length=255, null=True)

    def __str__(self):
        return self.display_name

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


class TaskHistory(Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField(
        "models.Account",
        related_name="task_history"
    )
    task_id = fields.CharField(max_length=255, unique=True)
    task_type = fields.CharField(max_length=50)
    parameters = fields.JSONField()
    status = fields.CharField(max_length=50, default="PENDING")
    result = fields.JSONField(null=True)  # Store task results or error details
    created_at = fields.DatetimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.task_type} - {self.task_id}"

    @classmethod
    async def finish(cls, task_id: str, state: str, result) -> bool:
        """
        Save a task's final Celery state and result, only the first time.

        The worker calls this when a task ends, and the API when it finds
        a finished task the worker couldn't save. The update only matches
        unfinished rows, so a failed task's credit is given back once.
        Returns False if the task has no row or was already finished.
        """
        if state not in states.READY_STATES:
            return False

        if state != states.SUCCESS:
            # A failed or revoked task's result is an exception. Keep only
            # its type: the message may expose details of the servers.
            result = {
                "status": "error",
                "message": "The task failed",
                "error": type(result).__name__,
            }

        # Encode it here: the JSON field can't store the datetimes, Decimals
        # and UUIDs that Celery decodes back into objects, and it takes any
        # str as JSON that is already encoded.
        encoded = json.dumps(result, default=str)

        async with in_transaction():
            updated = await cls.filter(task_id=task_id).exclude(
                status__in=states.READY_STATES
            ).update(status=state, result=encoded)

            if updated and state != states.SUCCESS:
                # Only successful tasks are charged.
                task = await cls.get(task_id=task_id).prefetch_related("user")
                await Account.refund_credit(task.user.google_id)

        return updated == 1
