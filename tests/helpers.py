"""Helpers shared by the tests."""
import asyncio
import datetime
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass

import asyncpg
import httpx
import jwt

# PyJWT warns about HS256 keys shorter than 32 bytes.
SECRET = "test-secret-not-for-production-use"

# The tests' admin: the email in the server's ADMINS, of the user with this Google ID.
ADMIN = "admin"
ADMIN_EMAIL = f"{ADMIN}@example.com"


def session_cookie(google_id, expires_in=datetime.timedelta(hours=1), key=SECRET, version=None):
    """
    A session JWT like the one login_callback() puts in the 'token' cookie,
    with the account's session version, or without one, like the sessions
    from before them.
    """
    pld = {
        "id": google_id,
        "email": f"{google_id}@example.com",
        "first_name": "Test",
        "last_name": "User",
        "display_name": "Test User",
        "picture": "https://example.com/picture.png",
        "provider": "google",
    }
    exp = datetime.datetime.now(tz=datetime.timezone.utc) + expires_in
    claims = {"pld": pld, "exp": exp, "sub": google_id}
    if version is not None:
        claims["ver"] = version
    return jwt.encode(claims, key=key, algorithm="HS256")


def client_for(server, cookie=None):
    """HTTP client for a test server, logged in when given a session cookie."""
    return httpx.AsyncClient(
        base_url=server.url,
        cookies={"token": cookie} if cookie else None,
        timeout=60,
        # No keep-alive: after an unhandled 500, uvicorn closes the connection.
        limits=httpx.Limits(max_keepalive_connections=0),
    )


@dataclass
class Account:
    id: int
    google_id: str
    api_key: str
    cookie: str
    db: asyncpg.Connection

    async def credits(self):
        return await self.db.fetchval("SELECT credits FROM account WHERE id = $1", self.id)

    async def set_credits(self, credits):
        await self.db.execute("UPDATE account SET credits = $1 WHERE id = $2", credits, self.id)


def stored_api_key(api_key):
    """How the database keeps an API key: "first 8 characters$SHA-256"."""
    return f"{api_key[:8]}${hashlib.sha256(api_key.encode()).hexdigest()}"


async def create_account(db, credits=5):
    google_id = f"test-{uuid.uuid4()}"
    api_key = secrets.token_hex(32)  # like generate_api_key() in main_api.py
    account_id = await db.fetchval(
        """
        INSERT INTO account (google_id, email, first_name, last_name, display_name,
                             picture, provider, created_at, credits, api_key)
        VALUES ($1, $2, 'Test', 'User', 'Test User', 'https://example.com/picture.png',
                'google', now(), $3, $4)
        RETURNING id
        """,
        google_id, f"{google_id}@example.com", credits, stored_api_key(api_key),
    )
    return Account(account_id, google_id, api_key, session_cookie(google_id), db)


async def insert_task(db, account, status="PENDING", result=None):
    """A history row, like the one start_task() creates before queueing."""
    task_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO taskhistory (task_id, task_type, parameters, status, result,
                                 created_at, user_id)
        VALUES ($1, 'divide', '{"x": 6, "y": 3}', $2, $3, now(), $4)
        """,
        task_id, status, None if result is None else json.dumps(result), account.id,
    )
    return task_id


async def age_task(db, task_id, age):
    """Move a task's creation back, as if it had been queued `age` ago."""
    await db.execute(
        "UPDATE taskhistory SET created_at = created_at - $2::interval WHERE task_id = $1",
        task_id, age,
    )


async def task_row(db, task_id):
    row = await db.fetchrow("SELECT status, result FROM taskhistory WHERE task_id = $1", task_id)
    return row["status"], None if row["result"] is None else json.loads(row["result"])


async def wait_for_row(db, task_id, status, timeout=30):
    """Wait for a task's row to reach a status, without going through the API."""
    deadline = time.monotonic() + timeout
    while True:
        row = await task_row(db, task_id)
        if row[0] == status or time.monotonic() > deadline:
            return row
        await asyncio.sleep(0.2)


async def wait_until_done(client, task_id, timeout=60):
    """Poll the task's details, like the details page does, until it ends."""
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/task_details_from_user/{task_id}")
        r.raise_for_status()
        data = r.json()
        if data["status"] != "PENDING" or time.monotonic() > deadline:
            return data
        await asyncio.sleep(0.5)


def skipped(task_id):
    """The worker's log line for a copy of a task that it takes, but doesn't run."""
    return f"Task {task_id} was started already, given up by the API, or deleted with its account: skipped."


async def wait_for_log(log, text, timeout=30):
    """Wait for a text to show up in a process's log."""
    deadline = time.monotonic() + timeout
    while text not in log.read_text():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{text!r} isn't in {log}")
        await asyncio.sleep(0.1)


def cookie_attributes(set_cookie):
    """A Set-Cookie header's attributes, without the value: {"httponly", "secure", ...}."""
    return {part.strip().lower() for part in set_cookie.split(";")[1:]}
