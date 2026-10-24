#!/usr/bin/env python3

import datetime
import hashlib
import json
import logging
import secrets
import uuid

from pydantic_core import to_jsonable_python

from tortoise.models import Model
from tortoise import fields, timezone
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F
from tortoise.transactions import in_transaction

logger = logging.getLogger(__name__)


def json_time(moment: datetime.datetime | None) -> str | None:
    """
    A time as NanoSaaS writes them all in JSON: ISO 8601, in UTC, ending in
    Z, as Pydantic writes the API's models, and as Standard Webhooks' examples.
    None stays None.
    """
    return None if moment is None else to_jsonable_python(moment.astimezone(datetime.timezone.utc))


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
    # Raised at each logout: the sessions from before it, on every device,
    # stop working (see get_logged_user()). Also a default of the column.
    session_version = fields.IntField(default=0, db_default=0)

    def __str__(self):
        return self.display_name

    @classmethod
    async def change_credits(
        cls, account_id: int, amount: int, reason: str, task_id: str | None = None
    ) -> int | None:
        """
        Add credits to an account, or take them with a negative amount, and
        record the movement (CreditMovement), with the balance it leaves, in
        the same transaction: every change of the credits goes through here.
        Taking them is a single UPDATE that needs the account to have them, so
        concurrent requests can't spend more credits than it has.
        Returns the balance, or None if there's no such account, or it didn't
        have that many credits.
        """
        async with in_transaction():
            enough = {"credits__gte": -amount} if amount < 0 else {}
            if not await cls.filter(id=account_id, **enough).update(credits=F("credits") + amount):
                return None
            # This transaction's own update: no other one changes it before this one ends.
            [balance] = await cls.filter(id=account_id).values_list("credits", flat=True)
            await CreditMovement.create(
                account_id=account_id, amount=amount, balance=balance, reason=reason, task_id=task_id
            )
        return balance

    @classmethod
    async def reserve_credits(cls, account_id: int, credits: int, task_id: str) -> bool:
        """Take a task's cost, when it starts. Returns False if the account didn't have that many."""
        return await cls.change_credits(account_id, -credits, "task", task_id) is not None

    @classmethod
    async def refund_credits(cls, account_id: int, credits: int, task_id: str) -> None:
        """Give back the credits a task took with reserve_credits()."""
        await cls.change_credits(account_id, credits, "refund", task_id)

    @classmethod
    async def delete_with_tasks(cls, google_id: str) -> bool:
        """
        Delete an account and its tasks, keeping only the hash of its
        Google ID (see DeletedAccount). Its sessions and its API keys stop
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


# How often the requests with an API key save when it was last used: the API key
# page shows it within a minute, without a write to the database on each request.
LAST_USE_PRECISION = datetime.timedelta(minutes=1)


class ApiKey(Model):
    """
    A key of an account to the API.

    An account can have several, each with a name: to change one without a
    moment without a key, and to have one for each client, so a leaked one
    can be revoked without stopping the others.

    The key itself isn't stored: it's shown once, when created. This keeps
    "first 8 characters$SHA-256" of it: the start tells keys apart, and the
    unique index finds the key's account.
    """
    id = fields.IntField(primary_key=True)
    account = fields.ForeignKeyField("models.Account", on_delete=fields.CASCADE, related_name="api_keys")
    # What its user calls it, like the client that uses it.
    name = fields.CharField(max_length=50)
    key_hash = fields.CharField(max_length=255, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    # When a request last came with it, within LAST_USE_PRECISION. None if none did.
    last_used_at = fields.DatetimeField(null=True)
    # A key that only reads the account's tasks, credits and history: starting or
    # canceling a task with it gets a 403 error. Also a default of the column: the
    # keys from before it can do everything.
    read_only = fields.BooleanField(default=False, db_default=False)

    @staticmethod
    def hash_key(key: str) -> str:
        """The stored form of a key (see key_hash)."""
        return f"{key[:8]}${hashlib.sha256(key.encode()).hexdigest()}"

    @property
    def prefix(self) -> str:
        """The start of the key, to tell which one it is."""
        return self.key_hash[:8]

    @classmethod
    async def generate(cls, account_id: int, name: str, limit: int, read_only: bool = False) -> str | None:
        """
        A new key of an account, with a name, read-only or not: the key itself,
        which only its hash is kept of. None if the account has `limit` keys
        already: its row stays locked meanwhile, so concurrent requests can't go
        over it.
        """
        key = secrets.token_hex(32)  # 64 characters
        async with in_transaction():
            # FOR NO KEY UPDATE, an UPDATE's lock: it doesn't hold up the rows that
            # only reference the account, like a new task's.
            await Account.select_for_update(no_key=True).get(id=account_id)
            if await cls.filter(account_id=account_id).count() >= limit:
                return None
            await cls.create(account_id=account_id, name=name, key_hash=cls.hash_key(key), read_only=read_only)
        return key

    @classmethod
    async def find(cls, key: str) -> "ApiKey | None":
        """
        A key, with its account, or None if the key isn't one, or was revoked.
        Saves when it was used, once every LAST_USE_PRECISION at most.
        """
        api_key = await cls.get_or_none(key_hash=cls.hash_key(key)).select_related("account")
        if api_key is None:
            return None
        now = timezone.now()
        if api_key.last_used_at is None or now - api_key.last_used_at >= LAST_USE_PRECISION:
            await cls.filter(id=api_key.id).update(last_used_at=now)
        return api_key


class CreditMovement(Model):
    """
    A change of an account's credits, with the balance it left.

    Account.change_credits() records each one, in the transaction of the
    change: the latest movement's balance is the account's credits.
    """
    id = fields.IntField(primary_key=True)
    account = fields.ForeignKeyField("models.Account", on_delete=fields.CASCADE, related_name="credit_movements")
    # Positive when credits come in, negative when they go out.
    amount = fields.IntField()
    balance = fields.IntField()
    # Why: "initial" (INITIAL_CREDITS, for a new account), "task" (a task's
    # cost, when it starts), "refund" (given back, when it fails, is canceled
    # or gets lost), "admin" (from the admin page), "script" (from
    # scripts/add_credits.py), or "opening" (the balance when the history began).
    reason = fields.CharField(max_length=20)
    # The task of a "task" or "refund" movement.
    task_id = fields.CharField(max_length=255, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        # For the pages of an account's history, the latest first, like the tasks'.
        # With the column of the account, which the migration takes as it is.
        indexes = (("account_id", "created_at", "id"),)


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
    # The credits it cost when it started, which it gives back if it fails,
    # is canceled or gets lost, whatever the task's cost is by then. Also a
    # default of the column, for the tasks from before costs: they cost 1.
    cost = fields.IntField(default=1, db_default=1)

    class Meta:
        unique_together = (("user", "idempotency_key"),)
        # For the pages of an account's history, the latest tasks first: each
        # one starts right after the last task of the one before. With the
        # column of the account, which the migration takes as it is.
        indexes = (("user_id", "created_at", "id"),)

    def __str__(self):
        return f"{self.task_type} - {self.task_id}"

    def details(self) -> dict:
        """The task as its account gets it: from /api/task_details, and in its webhook's events."""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "parameters": self.parameters,
            "status": self.status,
            "result": self.result,
            "created_at": json_time(self.created_at),
            "cost": self.cost,
        }

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
    async def release(cls, task_id: str, started_at: datetime.datetime, expired: bool = True) -> bool:
        """
        Release a task from its run that started at started_at, if its
        lease expired, so that it starts again when it's queued again.
        Without expired, the worker that stopped that run releases it.
        Returns False if that run ended, renewed its lease meanwhile, or
        another worker released it.
        """
        lease = {"lease_until__lt": timezone.now()} if expired else {}
        updated = await cls.filter(
            task_id=task_id, status="PENDING", started_at=started_at, **lease
        ).update(started_at=None)
        return updated == 1

    @classmethod
    async def give_up(cls, task_id: str) -> bool:
        """
        Delete a task that couldn't be queued and give its credits back,
        unless a worker already started it: then Redis did queue it, and
        only the reply to the API was lost. The credit history keeps both
        movements, the cost and its refund.
        Returns False if a worker started it.
        """
        async with in_transaction():
            # Locked: a worker that starts it meanwhile waits, and then finds it gone.
            task = await cls.select_for_update().get_or_none(
                task_id=task_id, status="PENDING", started_at__isnull=True
            )
            if task is not None:
                await task.delete()
                await Account.refund_credits(task.user_id, task.cost, task_id)

        return task is not None

    @classmethod
    async def cancel(cls, task_id: str, account: Account) -> bool:
        """
        Cancel an account's pending task that no worker started, and give
        its credits back. It stays in the history, as CANCELED: a worker
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
                task = await cls.get(task_id=task_id)
                await WebhookDelivery.record(task)
                await Account.refund_credits(account.id, task.cost, task_id)

        return canceled == 1

    @classmethod
    async def finish(cls, task_id: str, result=None, error: BaseException | None = None) -> bool:
        """
        Save a pending task's result, or the error it failed with, only
        the first time.

        The worker calls this when a task ends, and the API when it finds
        a finished task the worker couldn't save. The update only matches
        pending rows, so a failed task's credits are given back once.
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

            if updated:
                task = await cls.get(task_id=task_id)
                await WebhookDelivery.record(task)
                if error is not None:
                    # Only successful tasks are charged.
                    await Account.refund_credits(task.user_id, task.cost, task_id)

        return updated == 1


class Webhook(Model):
    """
    The URL where an account gets an event when each of its tasks ends.

    With the secret that signs the events (see webhooks.py). An account has
    one at most.
    """
    id = fields.IntField(primary_key=True)
    account = fields.OneToOneField("models.Account", on_delete=fields.CASCADE, related_name="webhook")
    url = fields.CharField(max_length=2000)
    # "whsec_" and the base64 of random bytes. Readable, unlike the API keys:
    # the worker signs each event with it.
    secret = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    # When the first of the attempts that keep failing was: None after one
    # that delivers its event. After DISABLE_AFTER, the worker disables it.
    failing_since = fields.DatetimeField(null=True)
    # When the worker disabled it, and why, as the pages show it: its events
    # wait, unsent, until its account enables it again (see webhooks.py).
    disabled_at = fields.DatetimeField(null=True)
    disabled_reason = fields.CharField(max_length=300, null=True)
    # Until when its events wait, as an answer asked, with a 429, a 502, a 504 or
    # a Retry-After (see webhooks.py). None, or a time that passed, while they don't.
    throttled_until = fields.DatetimeField(null=True)


class WebhookDelivery(Model):
    """
    An event for a webhook, which the worker sends until it's delivered.

    Or until it runs out of attempts (see webhooks.py). It's recorded in the
    same transaction as the change it tells of: none is lost, and none tells
    of a change that was undone.
    """
    id = fields.IntField(primary_key=True)
    webhook = fields.ForeignKeyField("models.Webhook", on_delete=fields.CASCADE, related_name="deliveries")
    # Its webhook-id header, the same in each attempt: receivers skip the ones they got.
    message_id = fields.CharField(max_length=50, unique=True)
    type = fields.CharField(max_length=50)
    # The task it tells of, if it's a task's event.
    task_id = fields.CharField(max_length=255, null=True)
    # The body, as it's sent and signed.
    payload = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)
    # PENDING until it's DELIVERED, or FAILED after its last attempt.
    status = fields.CharField(max_length=20, default="PENDING")
    attempts = fields.IntField(default=0)
    # When its next attempt is due. While a worker sends it, when the worker's claim on it ends.
    next_attempt_at = fields.DatetimeField()
    last_attempt_at = fields.DatetimeField(null=True)
    # How its last attempt went, like "200 OK" or "Timeout", or why it wasn't sent.
    last_response = fields.CharField(max_length=200, null=True)

    class Meta:
        # For the workers, which send the pending ones that are due, and for the
        # page with the latest of a webhook. With the columns, as the migration takes them.
        indexes = (("status", "next_attempt_at"), ("webhook_id", "created_at"))

    @classmethod
    async def queue(cls, webhook: Webhook, type: str, data: dict, task_id: str | None = None) -> None:
        """Record an event for a webhook, to be sent right away."""
        now = timezone.now()
        payload = {"type": type, "timestamp": json_time(now), "data": data}
        await cls.create(
            webhook=webhook, message_id=f"msg_{uuid.uuid4().hex}", type=type, task_id=task_id,
            payload=json.dumps(payload, separators=(",", ":")), next_attempt_at=now,
        )

    @classmethod
    async def record(cls, task: TaskHistory) -> None:
        """
        Record the task.ended event of a task that just ended, if its account
        has a webhook: TaskHistory.finish() and cancel() call it in their
        transactions, after the task's row and before the account's.
        """
        try:
            # In a savepoint, so nothing here makes the task's end fail: a webhook being
            # removed makes the insert wait for it, and then fail, and in a deploy, a new
            # worker may end a task before the API created the webhooks' tables.
            async with in_transaction():
                webhook = await Webhook.get_or_none(account_id=task.user_id)
                if webhook is not None:
                    await cls.queue(webhook, "task.ended", task.details(), task_id=task.task_id)
        except IntegrityError:
            pass
        except Exception:
            logger.exception("Can't record the webhook event of task %s", task.task_id)
