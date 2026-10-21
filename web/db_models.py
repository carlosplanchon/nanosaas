#!/usr/bin/env python3

import datetime
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
    # Raised at each logout: the sessions from before it, on every device,
    # stop working (see get_logged_user()). Also a default of the column.
    session_version = fields.IntField(default=0, db_default=0)

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

    @classmethod
    async def delete_with_tasks(cls, google_id: str) -> bool:
        """
        Delete an account and its tasks, keeping only the hash of its
        Google ID (see DeletedAccount). Its sessions and its API key stop
        working with it.

        The tasks go first and the account last, the order in which
        TaskHistory.finish() and give_up() lock them: a task that ends
        meanwhile makes this wait, instead of a deadlock. A worker that
        takes one of its queued tasks later skips it (see TaskHistory.start()).
        Returns False if there was no such account.
        """
        async with in_transaction():
            account = await cls.get_or_none(google_id=google_id)
            if account is None:
                return False
            await TaskHistory.filter(user_id=account.id).delete()
            await DeletedAccount.get_or_create(google_id_hash=DeletedAccount.hash_google_id(google_id))
            # The foreign key deletes, on cascade, the tasks created meanwhile.
            await cls.filter(id=account.id).delete()
        return True


class DeletedAccount(Model):
    """
    What's kept of a deleted account: the hash of its Google ID.

    Logging in with it again starts an account without INITIAL_CREDITS:
    otherwise, deleting the account would be a way to get them again.
    """
    id = fields.IntField(primary_key=True)
    google_id_hash = fields.CharField(max_length=64, unique=True)
    # When an account with this Google ID was deleted for the first time.
    deleted_at = fields.DatetimeField(auto_now_add=True)

    @staticmethod
    def hash_google_id(google_id: str) -> str:
        return hashlib.sha256(google_id.encode()).hexdigest()

    @classmethod
    async def exists_for(cls, google_id: str) -> bool:
        """Whether an account with this Google ID was deleted."""
        return await cls.filter(google_id_hash=cls.hash_google_id(google_id)).exists()


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
    # PENDING until the task ends as SUCCESS or FAILURE, or its user cancels
    # it, CANCELED, before a worker starts it. Indexed: every few seconds,
    # the workers look for the pending tasks whose lease expired.
    status = fields.CharField(max_length=50, default="PENDING", db_index=True)
    result = fields.JSONField(null=True)  # Store task results or error details
    created_at = fields.DatetimeField(auto_now_add=True)
    # When a worker took the task from the queue, for its current run (see start()).
    started_at = fields.DatetimeField(null=True)
    # Until when the worker running it holds its lease, which it renews while
    # it runs it (see renew()), and how many times a worker started it.
    lease_until = fields.DatetimeField(null=True)
    # Also a default of the column, which the existing rows get when it's added.
    attempts = fields.IntField(default=0, db_default=0)
    # The Idempotency-Key of the request that started the task, if it had
    # one: the account's requests with the same key get this task.
    idempotency_key = fields.CharField(max_length=255, null=True)

    class Meta:
        unique_together = (("user", "idempotency_key"),)
        # For the pages of an account's history, the latest tasks first: each
        # one starts right after the last task of the one before. With the
        # column of the account, which the migration takes as it is.
        indexes = (("user_id", "created_at", "id"),)

    def __str__(self):
        return f"{self.task_type} - {self.task_id}"

    @classmethod
    async def start(cls, task_id: str, lease: datetime.timedelta) -> datetime.datetime | None:
        """
        Mark a pending task as started, by one copy of it, with a lease
        that lasts lease.

        The worker calls this before it runs a task. Redis can queue a
        task twice, when it runs the LPUSH but the reply is lost and
        redis-py sends it again, or queue a task the API gave up for the
        same reason (see give_up()). Only one copy of a pending task
        starts: the first one, or after release(), the first one queued
        again.
        Returns when it started, which tells this run from the next ones
        (see renew()), or None if the task has no pending row, like one
        deleted with its account, or already started.
        """
        started_at = timezone.now()
        updated = await cls.filter(
            task_id=task_id, status="PENDING", started_at__isnull=True
        ).update(started_at=started_at, lease_until=started_at + lease, attempts=F("attempts") + 1)
        return started_at if updated else None

    @classmethod
    async def renew(cls, task_id: str, started_at: datetime.datetime, lease: datetime.timedelta) -> bool:
        """
        Renew for lease the lease of the run of a task that started at
        started_at. The worker calls this every few seconds while it runs
        the task.
        Returns False if that run ended, or was released.
        """
        updated = await cls.filter(
            task_id=task_id, status="PENDING", started_at=started_at
        ).update(lease_until=timezone.now() + lease)
        return updated == 1

    @classmethod
    async def release(cls, task_id: str, started_at: datetime.datetime) -> bool:
        """
        Release a task from its run that started at started_at, if its
        lease expired, so that it starts again when it's queued again.
        Returns False if that run ended, renewed its lease meanwhile, or
        another worker released it.
        """
        updated = await cls.filter(
            task_id=task_id, status="PENDING", started_at=started_at, lease_until__lt=timezone.now()
        ).update(started_at=None)
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
    async def cancel(cls, task_id: str, account: Account) -> bool:
        """
        Cancel an account's pending task that no worker started, and give
        its credit back. It stays in the history, as CANCELED: a worker
        that takes it from the queue skips it, as it only starts pending
        tasks (see start()). A worker that starts it meanwhile wins: then
        this doesn't cancel it.
        Returns False if the task isn't the account's, started or ended.
        """
        async with in_transaction():
            canceled = await cls.filter(
                task_id=task_id, user_id=account.id, status="PENDING", started_at__isnull=True
            ).update(status="CANCELED")
            if canceled:
                await Account.refund_credit(account.google_id)

        return canceled == 1

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
