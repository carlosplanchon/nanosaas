#!/usr/bin/env python3

from tortoise.models import Model
from tortoise import fields
from tortoise.expressions import F


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
