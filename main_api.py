#!/usr/bin/env python3

import asyncio

# to calculate expiration of the JWT
import datetime

import functools

import hashlib

import inspect

import json

import logging

import secrets

import sys

import tomllib

import typing

import uuid

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Depends, Header, HTTPException, Query, Security, Request

from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse

# this is the part that puts the lock icon to the docs
from fastapi.security import APIKeyCookie
from fastapi.security import APIKeyHeader, APIKeyQuery

from fastapi_sso.sso.google import GoogleSSO
from fastapi_sso.sso.base import OpenID

import jwt

from fastapi.templating import Jinja2Templates

from tortoise import Tortoise
from tortoise.contrib.fastapi import RegisterTortoise, tortoise_exception_handlers
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import F, Q
from tortoise.functions import Count
from tortoise.migrations.api import migrate
from tortoise.migrations.recorder import MigrationRecorder
from tortoise.transactions import in_transaction

from fastapi.staticfiles import StaticFiles

from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.responses import Response

from pydantic import BaseModel, ConfigDict, Field, create_model

from taskiq import AsyncTaskiqDecoratedTask
from taskiq_redis.exceptions import ResultIsMissingError

from taskiq_task import broker
from taskiq_task import ATTEMPTS, LEASE, QUEUE_TIMEOUT, TIME_LIMIT
from taskiq_task import LOG_FORMAT, TaskLost, ping_postgres, ping_redis, queue_task

from web.db_models import Account, CreditMovement, DeletedAccount, TaskHistory, Webhook, WebhookDelivery, json_time

from webhooks import KEEP as WEBHOOK_KEEP, Undeliverable, check_url, new_secret

from shared import CONFIG
from shared import CONFIG_PATH
from shared import TORTOISE_ORM


#############################
#                           #
#      --- FASTAPI ---      #
#                           #
#############################
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="web/templates")


root_path: str = CONFIG["app"]["ROOT_PATH"]

if root_path != "":
    root_prefix = f"/{root_path}"
else:
    root_prefix = ""

# The credits of a new account. Any Google account can log in: with 0,
# users start tasks only once you give them credits (scripts/add_credits.py).
INITIAL_CREDITS: int = CONFIG["app"].get("INITIAL_CREDITS", 25)

# The emails of the users who can open the admin page, /admin: their accounts'
# emails, which Google gave when each account was created (see is_admin()).
# Google only logs in users with their email verified (fastapi-sso checks it).
ADMINS: set[str] = {email.lower() for email in CONFIG["app"].get("ADMINS", [])}

# The most tasks an account can have pending, waiting in the queue or running.
# The workers take the tasks in the order they were queued: without a limit,
# an account with many credits could fill the queue, and the other accounts'
# tasks would wait behind its tasks, failing after queue_timeout.
MAX_PENDING_TASKS: int = CONFIG["app"].get("MAX_PENDING_TASKS", 10)

# The largest request body the API takes, in bytes. FastAPI reads the whole
# body of a route, and parses it, before it checks the API key or the session:
# without a limit, anyone could fill the API's memory (see BodySizeLimitMiddleware).
MAX_BODY_SIZE: int = CONFIG["app"].get("MAX_BODY_SIZE", 1024 * 1024)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uvicorn sets up only its own loggers: the app's lines go with their time, like the worker's.
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # Before RegisterTortoise: migrate() starts Tortoise again, which would undo it.
    await migrate_database()
    async with RegisterTortoise(app, config=TORTOISE_ORM):
        await broker.startup()
        try:
            yield
        finally:
            await broker.shutdown()


# NanoSaaS's version, from pyproject.toml: that of its release, tagged like
# v1.0.0. /docs shows it.
VERSION = tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text())["project"]["version"]

app = FastAPI(
    root_path=root_prefix,
    title="NanoSaaS",
    version=VERSION,
    lifespan=lifespan,
    exception_handlers=tortoise_exception_handlers()
)


TOO_LARGE = f"The request's body is larger than the limit of {MAX_BODY_SIZE} bytes."
# With the answer, the connection ends: the server doesn't read the rest of the body.
CLOSE = {"Connection": "close"}


class BodySizeLimitMiddleware:
    """
    Answer 413 to a request whose body is larger than MAX_BODY_SIZE, before
    the app reads more of it than that: right away when its Content-Length
    says so, and otherwise once that much of it arrives.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        length = Headers(scope=scope).get("content-length")
        if length is not None and int(length) > MAX_BODY_SIZE:
            return await JSONResponse({"detail": TOO_LARGE}, status_code=413, headers=CLOSE)(scope, receive, send)

        received = 0

        async def receive_within_limit():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_BODY_SIZE:
                # FastAPI raises it again from where it reads the body, and answers it like the one above.
                raise HTTPException(status_code=413, detail=TOO_LARGE, headers=CLOSE)
            return message

        await self.app(scope, receive_within_limit, send)


# Before SecurityHeadersMiddleware, which goes around it, so its answers get the headers too.
app.add_middleware(BodySizeLimitMiddleware)


# What the pages can load and run: only this server's files, without inline
# scripts or event handlers, the usual way injected HTML runs its code, and
# without eval: the pages load Alpine.js's CSP build, which reads the
# expressions of its attributes itself instead of running them as JavaScript.
# No site can show the pages in a frame, to trick users into clicking their buttons.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

# For every response: browsers take each file for the type it's served as,
# don't show it in a frame, and don't send its URL to other sites.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}

# The pages of the docs go without the policy: FastAPI makes them with inline
# scripts, which load Swagger UI and ReDoc from a CDN. With and without the
# ROOT_PATH, as the routes match them.
DOCS_PATHS = {
    prefix + path
    for path in (app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url) if path
    for prefix in ("", root_prefix)
}


class SecurityHeadersMiddleware:
    """Add SECURITY_HEADERS to every response, and CONTENT_SECURITY_POLICY to all but the docs'."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if scope["path"] not in DOCS_PATHS:
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


# Mount static files directory
STATIC_DIRECTORY = Path("web/static")
# How browsers and proxies keep a static file whose URL has the hash of what the
# server has, from static_url(): for a year, without asking for it again, even
# when the page reloads. The next version of the file has another URL.
CACHE_FOR_A_YEAR = "public, max-age=31536000, immutable"


class VersionedStaticFiles(StaticFiles):
    """
    The files of web/static, kept for a year when their URL has their hash
    (see static_url()). Not others, nor one with another hash: in a deploy,
    a page's new URL may reach a server that still has the old file.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        version = QueryParams(scope["query_string"]).get("v")
        # A 304 too, for a browser that asks again anyway.
        if version is not None and response.status_code in (200, 304):
            file = STATIC_DIRECTORY / path
            if version == content_version(file, file.stat().st_mtime_ns):
                response.headers["Cache-Control"] = CACHE_FOR_A_YEAR
        return response


app.mount("/web/static", VersionedStaticFiles(
        directory=STATIC_DIRECTORY
    ),
    name="static"
)


@functools.cache
def content_version(path: Path, modified: int) -> str:
    """The start of the file's SHA-256, once for each modification time it has."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def static_url(name: str) -> str:
    """
    The URL of a file of web/static for the templates, with ?v= and a hash
    of its contents. With the hash, a file's URL changes with it, so browsers
    keep each one for a year without asking for it again (see
    VersionedStaticFiles): after a deploy that changes it, they get the new one.
    """
    path = STATIC_DIRECTORY / name
    return f"{root_prefix}/web/static/{name}?v={content_version(path, path.stat().st_mtime_ns)}"


templates.env.globals["static_url"] = static_url


# Signs the sessions' JWTs: whoever knows it can sign a session of any user.
# HS256 needs a key of 32 bytes or more (RFC 7518, section 3.2), and PyJWT only
# warns about a shorter one, like the template's "...": the API doesn't start.
JWT_SIGNING_SECRET_KEY = CONFIG["api"]["JWT_SIGNING_SECRET_KEY"]
if len(JWT_SIGNING_SECRET_KEY) < 32:
    sys.exit(
        f"JWT_SIGNING_SECRET_KEY, in {CONFIG_PATH}, has {len(JWT_SIGNING_SECRET_KEY)} characters: "
        "it needs at least 32. Make one with: python3 scripts/generate_jwt_secret_key.py"
    )
GOOGLE_CLIENT_ID = CONFIG["api"]["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = CONFIG["api"]["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = CONFIG["api"]["GOOGLE_REDIRECT_URI"]

# Session cookie flags: HttpOnly hides the JWT from page scripts, Secure
# sends it only over HTTPS (or http://localhost, except in Safari) and
# SameSite=lax keeps it out of POSTs coming from other sites.
SESSION_COOKIE_FLAGS = {"httponly": True, "secure": True, "samesite": "lax"}


sso = GoogleSSO(
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    redirect_uri=GOOGLE_REDIRECT_URI
)


###########################
#                         #
#      --- TYPES ---      #
#                         #
###########################
# Response model for TaskIQ task status.
class TaskOut(BaseModel):
    id: str
    status: str


# Response model for TaskIQ task initiation.
class TaskInitOut(BaseModel):
    id: str
    message: str


# A task of the history.
class TaskHistoryItem(BaseModel):
    task_id: str
    task_type: str
    parameters: dict[str, typing.Any]
    status: str
    result: typing.Any
    created_at: datetime.datetime
    # Until a worker starts it, a pending task can be canceled.
    started: bool
    # The credits it cost when it started, given back if it failed or was canceled.
    cost: int


# A page of the history, the latest tasks first.
class TaskHistoryOut(BaseModel):
    tasks: list[TaskHistoryItem]
    # Whether there are older tasks: the next page has them.
    has_more: bool


###########################
#                         #
#      --- LOGIN ---      #
#                         #
###########################
def generate_api_key():
    return secrets.token_hex(32)  # Generates a 64-character API key


async def get_logged_user(
    cookie: str = Security(APIKeyCookie(name="token"))
        ) -> OpenID:
    """
    Get user's JWT stored in cookie 'token',
    parse it and return the user's OpenID.
    """
    try:
        claims = jwt.decode(
            cookie,
            key=JWT_SIGNING_SECRET_KEY,
            algorithms=["HS256"]
        )
        user = OpenID(**claims["pld"])
    except Exception as error:
        logger.info("Invalid session: %s", error)
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials"
        ) from error

    # A session from before the account's last logout doesn't work anymore,
    # nor one of a deleted account, even after its Google account logs in
    # again: that's another account, whose version starts over. The sessions
    # from before these claims have none: they count as the first version
    # of the current account.
    account = await Account.get_or_none(google_id=user.id)
    if (
        account is None
        or claims.get("ver", 0) != account.session_version
        or claims.get("acc", account.id) != account.id
    ):
        logger.info("Session of %s ended by a logout, or by deleting its account", user.id)
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials"
        )
    return user


class LoginRequired(Exception):
    """Raised by get_page_user so the page redirects to the login page."""


async def get_page_user(
    cookie: str | None = Security(APIKeyCookie(name="token", auto_error=False))
        ) -> OpenID:
    """
    Like get_logged_user, for the HTML pages: without a valid session,
    for example after the JWT expires, send the user to the login page
    instead of answering with a 401 error.
    """
    if cookie is None:
        raise LoginRequired()

    try:
        return await get_logged_user(cookie)
    except HTTPException as error:
        raise LoginRequired() from error


@app.exception_handler(LoginRequired)
async def redirect_to_login(request: Request, exc: LoginRequired):
    return RedirectResponse(url=f"{root_prefix}/")


async def is_admin(user: OpenID) -> bool:
    """
    Whether the user's account has an email of ADMINS: the account's, which
    Google gave when it was created, and not the session's. Whoever got
    JWT_SIGNING_SECRET_KEY could sign a session of their account with an
    admin's email, but one of an admin's account needs its Google ID.
    """
    account = await Account.get_or_none(google_id=user.id)
    return account is not None and account.email.lower() in ADMINS


async def get_admin(
    cookie: str | None = Security(APIKeyCookie(name="token", auto_error=False))
        ) -> OpenID:
    """
    The logged user, if one of ADMINS. For anyone else, with a session or
    without one, the admin routes answer 404, as if they didn't exist.
    """
    try:
        user = await get_logged_user(cookie) if cookie is not None else None
    except HTTPException:
        user = None
    if user is None or not await is_admin(user):
        raise HTTPException(status_code=404, detail="Not Found")
    return user


# The API key goes in the X-API-Key header. The api_key query parameter
# still works for older clients, but URLs end up in server and proxy logs.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)


async def get_api_key_account(
    header_key: str | None = Security(api_key_header),
    query_key: str | None = Security(api_key_query)
        ) -> Account:
    """Get the account of the API key, from the header or the query."""
    api_key = header_key or query_key
    account = await Account.get_by_api_key(api_key) if api_key else None
    if not account:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return account


@app.get("/", include_in_schema=False)
async def home(request: Request):
    """Render the login page."""
    return templates.TemplateResponse(
        request, "login.html", {
                "root_prefix": root_prefix
            }
        )


@app.get("/thankyou", response_class=HTMLResponse)
async def thankyou(request: Request):
    """Forget the user's session and return an HTML response."""
    response = templates.TemplateResponse(
        request, "thankyou.html", {
            "root_prefix": root_prefix
        }
    )
    return response


@app.get("/account_deleted", response_class=HTMLResponse)
async def account_deleted(request: Request):
    """The page after deleting the account."""
    return templates.TemplateResponse(
        request, "account_deleted.html", {
            "root_prefix": root_prefix
        }
    )


@app.get("/auth/login")
async def login():
    """Redirect the user to the Google login page."""
    async with sso:
        return await sso.get_login_redirect()


# POST because it ends the account's sessions: with GET, a link from another
# site could, since the SameSite=lax cookie goes with GET navigations.
@app.post("/auth/logout", response_class=HTMLResponse)
async def logout(
    cookie: str | None = Security(APIKeyCookie(name="token", auto_error=False))
        ):
    """End the user's sessions, on every device, and forget the cookie."""
    if cookie is not None:
        try:
            user = await get_logged_user(cookie)
        except HTTPException:
            pass  # A session that doesn't work has nothing to end.
        else:
            await Account.filter(google_id=user.id).update(session_version=F("session_version") + 1)

    # 303, so the browser follows it with a GET.
    response = RedirectResponse(url=f"{root_prefix}/thankyou", status_code=303)
    response.delete_cookie(key="token", **SESSION_COOKIE_FLAGS)
    return response


# POST, like logout: with GET, a link from another site could delete the
# account, since the SameSite=lax cookie goes with GET navigations.
@app.post("/user/delete_account", response_class=HTMLResponse)
async def delete_account(user: OpenID = Depends(get_logged_user)):
    """
    Delete the user's account, with its tasks, and forget the cookie. Its
    sessions, on every device, and its API key stop working.
    """
    if await Account.delete_with_tasks(user.id):
        logger.info("Account of %s deleted by its user", user.id)

    response = RedirectResponse(url=f"{root_prefix}/account_deleted", status_code=303)
    response.delete_cookie(key="token", **SESSION_COOKIE_FLAGS)
    return response


@app.get("/auth/callback")
async def login_callback(request: Request):
    """Process login and redirect the user to the protected endpoint."""
    async with sso:
        openid = await sso.verify_and_process(request)
        if not openid:
            raise HTTPException(
                status_code=401,
                detail="Authentication failed"
            )

    # Check if account exists, if not, create it: without credits if the
    # Google account had one that was deleted (see DeletedAccount). A new
    # account's credits start its credit history.
    async with in_transaction():
        account, created = await Account.get_or_create(
            google_id=openid.id,
            defaults={
                "email": openid.email,
                "first_name": openid.first_name,
                "last_name": openid.last_name,
                "display_name": openid.display_name,
                "picture": openid.picture,
                "provider": openid.provider,
                "credits": 0 if await DeletedAccount.exists_for(openid.id) else INITIAL_CREDITS,
            },
        )
        if created and account.credits:
            await CreditMovement.create(
                account=account, amount=account.credits, balance=account.credits, reason="initial"
            )

    # Create a JWT with the user's OpenID
    expiration = datetime.datetime.now(
        tz=datetime.timezone.utc
            ) + datetime.timedelta(days=1)
    token = jwt.encode(
        {
            "pld": openid.model_dump(),
            "exp": expiration,
            "sub": openid.id,
            # The account's sessions from before its next logout.
            "ver": account.session_version,
            # The account's: if it's deleted, the next one of the same Google account is another.
            "acc": account.id
        },
        key=JWT_SIGNING_SECRET_KEY,
        algorithm="HS256"
    )
    response = RedirectResponse(url=f"{root_prefix}/userpanel")
    response.set_cookie(
        key="token",
        value=token,
        expires=expiration,
        **SESSION_COOKIE_FLAGS
    )
    return response


###############################
#                             #
#      --- USER INFO ---      #
#                             #
###############################
@app.get("/user/credits")
async def check_credits(user: OpenID = Depends(get_logged_user)):
    """Check the logged user's remaining credits."""
    await refresh_overdue_tasks(user.id)
    account = await Account.get(google_id=user.id)
    return {"credits": account.credits}


@app.get("/api/user_credits", response_model=dict)
async def get_user_credits_with_key(
    account: Account = Depends(get_api_key_account)
        ):
    """
    Get the user's remaining credits using an API key.

    Returns:
        dict: The number of remaining credits.
    """
    await refresh_overdue_tasks(account.google_id)
    await account.refresh_from_db(fields=["credits"])
    return {"credits": account.credits}


# How many tasks a page of the history has, the latest first, unless the
# request asks for another number, up to TASK_HISTORY_MAX_LIMIT.
TASK_HISTORY_LIMIT = 50
TASK_HISTORY_MAX_LIMIT = 100

# The query parameters that pick a page of the history.
HistoryBefore = typing.Annotated[str | None, Query(
    description="The task_id of the last task of the previous page: this page has the tasks before it."
)]
HistoryLimit = typing.Annotated[int, Query(
    ge=1, le=TASK_HISTORY_MAX_LIMIT, description="How many tasks the page has, at most."
)]


async def task_history_page(account: Account, before: str | None, limit: int) -> TaskHistoryOut:
    """
    A page of the account's history, the latest tasks first: the first
    one, or the one with the tasks before the task `before`, which the
    previous page ends with. Shared by the routes for the panel and for
    API keys. Its pending tasks are refreshed, as in the task details.

    A page starts right after its `before` task, so the tasks started
    meanwhile don't move it: they show up in the first page.
    """
    tasks = TaskHistory.filter(user=account)
    if before:
        last = await TaskHistory.get_or_none(task_id=before, user=account)
        if last is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found or you do not have permission to access it"
            )
        # Older than it, or as old with a lower id. The created_at__lte, which
        # adds nothing to that, lets PostgreSQL start reading the history's
        # index at it, instead of reading and skipping the newer tasks.
        tasks = tasks.filter(
            Q(created_at__lt=last.created_at) | Q(created_at=last.created_at, id__lt=last.id),
            created_at__lte=last.created_at
        )

    # One more than the page: if it's there, there are more.
    page = tasks.order_by("-created_at", "-id").limit(limit + 1)
    await refresh_tasks(await page)

    # Read it again, after the updates.
    rows = await page
    return TaskHistoryOut(
        tasks=[history_item(task) for task in rows[:limit]],
        has_more=len(rows) > limit
    )


def history_item(task: TaskHistory) -> TaskHistoryItem:
    """A task as the history gives it: in its pages, and in the account's export."""
    return TaskHistoryItem(
        task_id=task.task_id,
        task_type=task.task_type,
        parameters=task.parameters,
        status=task.status,
        result=task.result,
        created_at=task.created_at,
        started=task.started_at is not None,
        cost=task.cost
    )


@app.get("/user/task_history", response_model=TaskHistoryOut)
async def task_history(
    before: HistoryBefore = None,
    limit: HistoryLimit = TASK_HISTORY_LIMIT,
    user: OpenID = Depends(get_logged_user)
        ):
    """Retrieve a page of the logged user's tasks, the latest first."""
    account = await Account.get(google_id=user.id)
    return await task_history_page(account, before, limit)


@app.get("/api/task_history", response_model=TaskHistoryOut)
async def task_history_with_api_key(
    before: HistoryBefore = None,
    limit: HistoryLimit = TASK_HISTORY_LIMIT,
    account: Account = Depends(get_api_key_account)
        ):
    """
    List the account's tasks, the latest first, a page at a time. While
    has_more is true, there are older ones: for the next page, send the
    task_id of the last task of this one as before.
    """
    return await task_history_page(account, before, limit)


# The pages of the credit history, like the tasks'.
CREDIT_HISTORY_LIMIT = 50

CreditsBefore = typing.Annotated[int | None, Query(
    description="The id of the last movement of the previous page: this page has the movements before it."
)]
CreditsLimit = typing.Annotated[int, Query(
    ge=1, le=TASK_HISTORY_MAX_LIMIT, description="How many movements the page has, at most."
)]


# A change of the account's credits.
class CreditMovementItem(BaseModel):
    id: int
    # Positive when credits came in, negative when they went out.
    amount: int
    # The account's credits after it.
    balance: int
    # Why: "initial", "task", "refund", "admin", "script" or "opening" (see CreditMovement).
    reason: str
    # The task of a "task" or "refund" movement.
    task_id: str | None
    created_at: datetime.datetime


class CreditHistoryOut(BaseModel):
    movements: list[CreditMovementItem]
    has_more: bool


async def credit_history_page(account: Account, before: int | None, limit: int) -> CreditHistoryOut:
    """
    A page of the account's credit history, the latest movements first: the
    first one, or the one with the movements before the movement `before`,
    as task_history_page() pages the tasks. The pending tasks past their
    deadline are refreshed first, so their refunds show.
    """
    await refresh_overdue_tasks(account.google_id)
    movements = CreditMovement.filter(account_id=account.id)
    if before is not None:
        last = await CreditMovement.get_or_none(id=before, account_id=account.id)
        if last is None:
            raise HTTPException(status_code=404, detail="Credit movement not found")
        movements = movements.filter(
            Q(created_at__lt=last.created_at) | Q(created_at=last.created_at, id__lt=last.id),
            created_at__lte=last.created_at
        )

    # One more than the page: if it's there, there are more.
    rows = await movements.order_by("-created_at", "-id").limit(limit + 1)
    return CreditHistoryOut(
        movements=[movement_item(movement) for movement in rows[:limit]],
        has_more=len(rows) > limit
    )


def movement_item(movement: CreditMovement) -> CreditMovementItem:
    """A credit movement as the credit history gives it: in its pages, and in the account's export."""
    return CreditMovementItem(
        id=movement.id,
        amount=movement.amount,
        balance=movement.balance,
        reason=movement.reason,
        task_id=movement.task_id,
        created_at=movement.created_at,
    )


@app.get("/user/credit_history", response_model=CreditHistoryOut)
async def credit_history(
    before: CreditsBefore = None,
    limit: CreditsLimit = CREDIT_HISTORY_LIMIT,
    user: OpenID = Depends(get_logged_user)
        ):
    """Retrieve a page of the logged user's credit movements, the latest first."""
    account = await Account.get(google_id=user.id)
    return await credit_history_page(account, before, limit)


@app.get("/api/credit_history", response_model=CreditHistoryOut)
async def credit_history_with_api_key(
    before: CreditsBefore = None,
    limit: CreditsLimit = CREDIT_HISTORY_LIMIT,
    account: Account = Depends(get_api_key_account)
        ):
    """
    List each change of the account's credits, the latest first, with the
    balance it left: its initial credits, each task's cost and each refund,
    and the ones added to it. A page at a time: while has_more is true,
    there are older ones; for the next page, send the id of the last
    movement of this one as before.
    """
    return await credit_history_page(account, before, limit)


@app.get("/user/api_key", response_model=dict)
async def get_api_key(user: OpenID = Depends(get_logged_user)):
    """
    Fetch the start of the logged-in user's API key. Only its hash is
    stored, so the whole key is shown just once, when it's generated.
    """
    account = await Account.get(google_id=user.id)
    return {"api_key_prefix": account.api_key_prefix}


@app.post("/user/api_key", response_model=dict)
async def create_or_regenerate_api_key(
    user: OpenID = Depends(get_logged_user)
        ):
    account = await Account.get(google_id=user.id)

    # Generate a new API key
    new_api_key = generate_api_key()
    account.api_key_hash = Account.hash_api_key(new_api_key)
    # Save only this field: a full save would also write back the
    # credits read above, undoing any credit reserved meanwhile.
    await account.save(update_fields=["api_key_hash"])

    # The only time the whole key is shown: it isn't stored.
    return {"api_key": new_api_key}


# The latest deliveries of a webhook that the API key page shows, with these
# fields: without their payloads.
WEBHOOK_DELIVERIES_SHOWN = 20
DELIVERY_FIELDS = ("id", "message_id", "type", "task_id", "status", "attempts", "created_at", "last_response")
# How often an account can send a test event to its webhook.
WEBHOOK_TEST_INTERVAL = datetime.timedelta(seconds=10)
# A webhook as it is while it works, which enabling it again, or saving its URL,
# sets: not disabled, without failures, and with its events not waiting (see webhooks.py).
WEBHOOK_WORKING = {"failing_since": None, "disabled_at": None, "disabled_reason": None, "throttled_until": None}


class WebhookIn(BaseModel):
    url: str


async def webhook_of(account: Account) -> dict:
    """The account's webhook as the API key page shows it, with its latest deliveries."""
    webhook = await Webhook.get_or_none(account_id=account.id)
    if webhook is None:
        return {"url": None, "secret": None, "disabled_at": None, "disabled_reason": None, "failed": 0, "deliveries": []}
    deliveries = await WebhookDelivery.filter(webhook_id=webhook.id).order_by(
        "-created_at", "-id"
    ).limit(WEBHOOK_DELIVERIES_SHOWN).only(*DELIVERY_FIELDS)
    return {
        "url": webhook.url,
        "secret": webhook.secret,
        # When the worker disabled it, and why: meanwhile, its events wait.
        "disabled_at": json_time(webhook.disabled_at),
        "disabled_reason": webhook.disabled_reason,
        # The events that failed, which the page can send again.
        "failed": await WebhookDelivery.filter(webhook_id=webhook.id, status="FAILED").count(),
        "deliveries": [delivery_item(delivery) for delivery in deliveries],
    }


def delivery_item(delivery: WebhookDelivery) -> dict:
    """A delivery of the webhook as the API key page shows it, and the account's export."""
    return {
        "id": delivery.message_id,
        "type": delivery.type,
        "task_id": delivery.task_id,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "created_at": json_time(delivery.created_at),
        # The receiver's answer, from another server: the page shows it as text.
        "last_response": delivery.last_response,
    }


# The webhook's routes take only the session, like the API key's: a leaked key
# can't send the account's results somewhere else. Out of /docs, like the panel's.
@app.get("/user/webhook", include_in_schema=False)
async def get_webhook(user: OpenID = Depends(get_logged_user)):
    return await webhook_of(await Account.get(google_id=user.id))


@app.put("/user/webhook", include_in_schema=False)
async def set_webhook(webhook_in: WebhookIn, user: OpenID = Depends(get_logged_user)):
    """Set the URL of the account's webhook. A new one gets a secret, which signs its events."""
    try:
        await check_url(webhook_in.url)
    except Undeliverable as e:
        raise HTTPException(status_code=422, detail=f"{e}.")
    account = await Account.get(google_id=user.id)
    webhook, created = await Webhook.get_or_create(
        account=account, defaults={"url": webhook_in.url, "secret": new_secret()}
    )
    if not created:
        # The pending deliveries go to the new URL. Saving it enables the webhook
        # again, if the worker disabled it, with its failures forgotten.
        await Webhook.filter(id=webhook.id).update(url=webhook_in.url, **WEBHOOK_WORKING)
    return await webhook_of(account)


@app.delete("/user/webhook", include_in_schema=False)
async def remove_webhook(user: OpenID = Depends(get_logged_user)):
    """Remove the account's webhook, with its deliveries: the pending ones aren't sent."""
    account = await Account.get(google_id=user.id)
    await Webhook.filter(account_id=account.id).delete()
    return await webhook_of(account)


@app.post("/user/webhook/secret", include_in_schema=False)
async def new_webhook_secret(user: OpenID = Depends(get_logged_user)):
    """Replace the webhook's secret: it signs the next attempts, of the pending deliveries too."""
    account = await Account.get(google_id=user.id)
    if not await Webhook.filter(account_id=account.id).update(secret=new_secret()):
        raise HTTPException(status_code=404, detail="Set a webhook URL first.")
    return await webhook_of(account)


async def enabled_webhook(account: Account) -> Webhook:
    """The account's webhook, to send events to: 404 without one, and 409 while it's disabled, when they'd wait."""
    webhook = await Webhook.get_or_none(account_id=account.id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Set a webhook URL first.")
    if webhook.disabled_at is not None:
        raise HTTPException(status_code=409, detail="Enable the webhook first.")
    return webhook


@app.post("/user/webhook/test", include_in_schema=False)
async def test_webhook(user: OpenID = Depends(get_logged_user)):
    """
    Send a webhook.test event, to see how the receiver gets the events. It
    replaces a pending one, and it can be sent once every WEBHOOK_TEST_INTERVAL:
    otherwise, users could send any number of requests anywhere.
    """
    account = await Account.get(google_id=user.id)
    webhook = await enabled_webhook(account)
    tests = WebhookDelivery.filter(webhook_id=webhook.id, type="webhook.test")
    if await tests.filter(created_at__gt=datetime.datetime.now(tz=datetime.timezone.utc) - WEBHOOK_TEST_INTERVAL).exists():
        raise HTTPException(
            status_code=429,
            detail=f"Wait {WEBHOOK_TEST_INTERVAL.seconds} seconds between test events.",
        )
    await tests.filter(status="PENDING").delete()
    await WebhookDelivery.queue(webhook, "webhook.test", {})
    return await webhook_of(account)


@app.post("/user/webhook/enable", include_in_schema=False)
async def enable_webhook(user: OpenID = Depends(get_logged_user)):
    """Enable the webhook again, after the worker disabled it: the events that wait go out."""
    account = await Account.get(google_id=user.id)
    if not await Webhook.filter(account_id=account.id).update(**WEBHOOK_WORKING):
        raise HTTPException(status_code=404, detail="Set a webhook URL first.")
    return await webhook_of(account)


@app.post("/user/webhook/resend", include_in_schema=False)
async def resend_webhook_events(user: OpenID = Depends(get_logged_user)):
    """
    Send the webhook's failed events again, as new, with their IDs: for
    example after an outage of the receiver, which skips the ones it got.
    """
    account = await Account.get(google_id=user.id)
    webhook = await enabled_webhook(account)
    await WebhookDelivery.filter(webhook_id=webhook.id, status="FAILED").update(
        status="PENDING", attempts=0, next_attempt_at=datetime.datetime.now(tz=datetime.timezone.utc),
        last_attempt_at=None, last_response=None,
    )
    return await webhook_of(account)


# The rows the account's export reads at a time: it sends them as it reads them,
# instead of holding the account's whole history in memory. As many as the
# largest page of the history, so the event loop doesn't spend longer on each
# one, without serving the other requests, than on a page of the history.
EXPORT_PAGE = TASK_HISTORY_MAX_LIMIT


async def pages_in_order(rows) -> typing.AsyncIterator[list]:
    """
    The rows of a query, the oldest first, EXPORT_PAGE at a time. Each page
    starts right after the last row of the one before, as the history's
    pages do, so the rows added meanwhile, the newest, come in the last one.
    """
    page = await rows.order_by("created_at", "id").limit(EXPORT_PAGE)
    while page:
        yield page
        if len(page) < EXPORT_PAGE:
            return
        last = page[-1]
        page = await rows.filter(
            Q(created_at__gt=last.created_at) | Q(created_at=last.created_at, id__gt=last.id),
            created_at__gte=last.created_at
        ).order_by("created_at", "id").limit(EXPORT_PAGE)


def compact_json(value) -> str:
    """JSON without spaces, as Pydantic writes it, and with each character as it is, in UTF-8."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def json_array(pages: typing.AsyncIterator[list], encode: typing.Callable) -> typing.AsyncIterator[str]:
    """A JSON array of the rows of the pages, each as encode() writes it, one a line, in a piece for each page."""
    separator = "\n    "
    yield "["
    async for page in pages:
        yield separator + ",\n    ".join(map(encode, page))
        separator = ",\n    "
    yield "\n  ]" if separator == ",\n    " else "]"


async def account_export(account: Account) -> typing.AsyncIterator[str]:
    """
    The account's data, as a JSON document, in pieces: its profile and its
    webhook, and its tasks, credit history and webhook events, as the API
    and the API key page give them, the oldest first.
    """
    webhook = await Webhook.get_or_none(account_id=account.id)
    head = {
        "exported_at": json_time(datetime.datetime.now(tz=datetime.timezone.utc)),
        "account": {
            "google_id": account.google_id,
            "email": account.email,
            "first_name": account.first_name,
            "last_name": account.last_name,
            "display_name": account.display_name,
            "picture": account.picture,
            "provider": account.provider,
            "created_at": json_time(account.created_at),
            "credits": account.credits,
            # Only the start of the API key, as the API key page shows it: the key isn't
            # stored, and its hash is no use to anyone else.
            "api_key_prefix": account.api_key_prefix,
        },
        # Without its secret, which signs its events: a credential, like the API key.
        "webhook": None if webhook is None else {
            "url": webhook.url,
            "created_at": json_time(webhook.created_at),
            "disabled_at": json_time(webhook.disabled_at),
            "disabled_reason": webhook.disabled_reason,
        },
    }
    # Each row's JSON, written by Pydantic's own encoder where the API uses its models: a
    # few times faster than json.dumps() of model_dump().
    lists = [
        ("tasks", TaskHistory.filter(user=account), lambda task: history_item(task).model_dump_json()),
        (
            "credit_history", CreditMovement.filter(account_id=account.id),
            lambda movement: movement_item(movement).model_dump_json(),
        ),
        (
            "webhook_events",
            None if webhook is None else WebhookDelivery.filter(webhook_id=webhook.id).only(*DELIVERY_FIELDS),
            lambda delivery: compact_json(delivery_item(delivery)),
        ),
    ]

    yield "{\n"
    for name, value in head.items():
        yield f"  {json.dumps(name)}: {compact_json(value)},\n"
    for index, (name, rows, encode) in enumerate(lists):
        yield f"  {json.dumps(name)}: "
        if rows is None:
            yield "[]"
        else:
            async for piece in json_array(pages_in_order(rows), encode):
                yield piece
        yield ",\n" if index < len(lists) - 1 else "\n}\n"


# Only with the session, like the webhook's routes: a leaked API key can't get the
# account's profile, nor its webhook's URL, which may have a token in it. Out of
# /docs, like the panel's.
@app.get("/user/export", include_in_schema=False)
async def export_account(user: OpenID = Depends(get_page_user)):
    """Download the account's data as a JSON file (see account_export()), sent as it's read."""
    # Its pending tasks first, as the history's pages refresh theirs: the lost ones fail,
    # and their credits come back.
    await refresh_tasks(await TaskHistory.filter(user__google_id=user.id, status="PENDING"))
    account = await Account.get(google_id=user.id)
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    return StreamingResponse(
        account_export(account),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="nanosaas-account-{today}.json"',
            # It has the account's data: no cache keeps a copy.
            "Cache-Control": "no-store",
        },
    )


@app.get("/user/api_key_page", response_class=HTMLResponse)
async def api_key_page(
    request: Request,
    user: OpenID = Depends(get_page_user)
        ):
    """
    Serve the API key management page.
    """
    return templates.TemplateResponse(
        request,
        "api_key_page.html",
        {
            "root_prefix": root_prefix,
            # How long the events of a disabled webhook wait.
            "webhook_keep_days": WEBHOOK_KEEP.days,
        }
    )


############################
#                          #
#      --- TASKIQ ---      #
#                          #
############################
@app.get("/userpanel", response_class=HTMLResponse)
async def serve_page(
    request: Request,
    user: OpenID = Depends(get_page_user)
        ):
    return templates.TemplateResponse(
        request, "user_panel.html", {
            "root_prefix": root_prefix,
            # Admins get a button to the admin page.
            "is_admin": await is_admin(user),
            # And users whose webhook the worker disabled, a notice.
            "webhook_disabled": await Webhook.filter(account__google_id=user.id, disabled_at__isnull=False).exists(),
        }
    )


###########################
#                         #
#      --- TASKS ---      #
#                         #
###########################
# Past this, a pending task was lost: TaskIQ can't start or run it anymore. In each
# of its runs it can wait in the queue and run until it's stopped, and a run whose
# worker died is queued again within twice its lease.
TASK_DEADLINE = datetime.timedelta(seconds=ATTEMPTS * (QUEUE_TIMEOUT + TIME_LIMIT + 2 * LEASE) + 60)
# Sooner for a task that never started: past its time in the queue, a worker would drop it.
QUEUE_DEADLINE = datetime.timedelta(seconds=QUEUE_TIMEOUT + 60)


def task_cost(task: AsyncTaskiqDecoratedTask) -> int:
    """
    The credits a task costs: its cost label, as in
    @broker.task(task_name="render", cost=5), or 1 without one.
    """
    cost = task.labels.get("cost", 1)
    # Not a bool either, which Python takes for an int.
    if type(cost) is not int or cost < 1:
        raise ValueError(f"The cost of the task {task.task_name} isn't a whole number of credits, 1 or more: {cost!r}")
    return cost


@functools.cache
def parameters_model(task_name: str) -> type[BaseModel]:
    """
    A model of the task's parameters, from its function's signature and
    type hints: every parameter except user_id, which start_task() adds,
    and task_id, which the worker adds.
    """
    function = broker.find_task(task_name).original_func
    hints = typing.get_type_hints(function)
    fields = {
        name: (
            hints.get(name, typing.Any),
            ... if parameter.default is inspect.Parameter.empty else parameter.default
        )
        for name, parameter in inspect.signature(function).parameters.items()
        if name not in ("user_id", "task_id")
    }
    return create_model(
        f"{task_name}_parameters", __config__=ConfigDict(extra="forbid"), **fields
    )


# A value the client picks for each request, like a UUID, and sends again
# when it retries the request: then it gets the task the request started.
IdempotencyKey = typing.Annotated[str | None, Header(
    alias="Idempotency-Key",
    min_length=1,
    max_length=255,
    description=(
        "A value you pick for this request, like a UUID. If you send the request again "
        "with it, for example after a timeout, you get the task it started, instead "
        "of starting and paying for another one."
    ),
)]


async def start_task(
    account: Account,
    task: AsyncTaskiqDecoratedTask,
    parameters: dict,
    idempotency_key: str | None = None
) -> TaskInitOut:
    """
    Reserve the task's cost in credits (see task_cost()), save the task
    in history and queue it in TaskIQ, with its parameters and the user's
    ID. Shared by every route that starts a task.

    The credits are taken before queueing, so pending tasks can never
    add up to more than the account's credits. They are given back if
    the task fails, so only successful runs are charged.

    A request with the idempotency key of a task the account already
    started gets that task, without paying again.

    An account with MAX_PENDING_TASKS pending tasks can't start another
    until one of them ends: it gets a 429 error.
    """
    await refresh_overdue_tasks(account.google_id)

    task_id = str(uuid.uuid4())
    cost = task_cost(task)
    # The credits and the row together: every credit taken has a pending
    # row, which gives it back if the task fails or gets lost. The row is
    # saved before the task is queued, so the worker always finds it.
    async with in_transaction():
        # Lock the account until this ends: the requests sent meanwhile wait
        # here, so a retry finds this request's task, and the others count it
        # among the pending ones.
        await Account.select_for_update().get(id=account.id)
        if idempotency_key is not None:
            started = await TaskHistory.get_or_none(user=account, idempotency_key=idempotency_key)
            if started is not None:
                if (started.task_type, started.parameters) != (task.task_name, parameters):
                    raise HTTPException(
                        status_code=422,
                        detail="This Idempotency-Key was used for another task or other parameters"
                    )
                return TaskInitOut(id=started.task_id, message="Task started successfully")

        if await TaskHistory.filter(user=account, status="PENDING").count() >= MAX_PENDING_TASKS:
            raise HTTPException(
                status_code=429,
                detail=f"Too many pending tasks: an account can have up to {MAX_PENDING_TASKS}. Wait for one to end."
            )
        if not await Account.reserve_credits(account.id, cost, task_id):
            raise HTTPException(status_code=403, detail="Not enough credits")
        await TaskHistory.create(
            user=account,
            task_id=task_id,
            task_type=task.task_name,
            parameters=parameters,
            status="PENDING",
            idempotency_key=idempotency_key,
            cost=cost
        )

    try:
        await queue_task(task.task_name, task_id, parameters, account.google_id)
    except Exception as e:
        # The error goes to the log: it may show where the servers are.
        logger.error("Task initiation failed for %s: %s", task_id, e.__cause__ or e)

        # Redis may have queued it anyway, and only lost its reply. Give
        # the task up, unless a worker already started it.
        if await TaskHistory.give_up(task_id):
            raise HTTPException(
                status_code=500,
                detail="Task initiation failed"
            )
        logger.warning("Task %s was queued after all: a worker started it.", task_id)

    return TaskInitOut(id=task_id, message="Task started successfully")


async def refresh_task(task_row: TaskHistory) -> None:
    """
    Finish a pending task from TaskIQ's result backend.

    The worker saves each task when it ends. This covers the ones it
    couldn't save (for example, with the database down), as long as the
    result backend still has their result: it forgets results after a day.
    A task that never ends fails at its deadline, giving its credits back.
    """
    if task_row.status != "PENDING":
        return

    try:
        result = await broker.result_backend.get_result(task_row.task_id)
    except ResultIsMissingError:
        never_started = task_row.started_at is None and task_row.attempts == 0
        deadline = QUEUE_DEADLINE if never_started else TASK_DEADLINE
        if task_row.created_at < datetime.datetime.now(tz=datetime.timezone.utc) - deadline:
            await TaskHistory.finish(task_row.task_id, error=TaskLost())
    else:
        await TaskHistory.finish(task_row.task_id, result.return_value, result.error)

    await task_row.refresh_from_db(fields=["status", "result"])


async def refresh_tasks(task_rows: list[TaskHistory]) -> None:
    """refresh_task() for each row, skipping the ones that fail."""
    for task_row in task_rows:
        try:
            await refresh_task(task_row)
        except Exception as e:
            logger.error("Error updating task %s: %s", task_row.task_id, e)


async def refresh_overdue_tasks(google_id: str) -> None:
    """Refresh the account's pending tasks past their deadline, so the credits are right."""
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    overdue = await TaskHistory.filter(
        Q(created_at__lt=now - TASK_DEADLINE)
        | Q(created_at__lt=now - QUEUE_DEADLINE, started_at__isnull=True, attempts=0),
        user__google_id=google_id,
        status="PENDING",
    )
    await refresh_tasks(overdue)


def input_schema(schema: dict, definitions: dict) -> dict:
    """
    The part of a parameter's schema that picks its input in the panel:
    the schema it references (a model's or an enum's, in $defs), the one
    besides null in an optional parameter's, or else the schema itself.
    """
    if "$ref" in schema:
        return input_schema(definitions[schema["$ref"].rsplit("/", 1)[-1]], definitions)
    alternatives = [option for option in schema.get("anyOf", []) if option.get("type") != "null"]
    if len(alternatives) == 1:
        return input_schema(alternatives[0], definitions)
    return schema


def parameter_input(name: str, field: dict, definitions: dict) -> dict:
    """
    What the panel needs for a parameter's input: its JSON type ("string"
    when there isn't one, as in a union of several types), the values to
    choose from, of an enum or a Literal (a Literal of one has a "const"),
    and its default.
    """
    schema = input_schema(field, definitions)
    options = schema.get("enum") or ([schema["const"]] if "const" in schema else None)
    return {"name": name, "type": schema.get("type", "string"), "options": options, "default": field.get("default")}


@app.get("/user/tasks")
async def list_tasks(user: OpenID = Depends(get_logged_user)):
    """The tasks the user can start, with their description, parameters and cost."""
    tasks = []
    for name, task in sorted(broker.get_all_tasks().items()):
        schema = parameters_model(name).model_json_schema()
        definitions = schema.get("$defs", {})
        tasks.append({
            "name": name,
            "description": inspect.getdoc(task.original_func) or "",
            "cost": task_cost(task),
            "parameters": [
                parameter_input(parameter, field, definitions)
                for parameter, field in schema.get("properties", {}).items()
            ],
        })
    return tasks


def add_task_routes(name: str, task: AsyncTaskiqDecoratedTask) -> None:
    """
    Add the routes that start a task, with its parameters in a JSON
    object: POST /api/task/<name> for API keys, which /docs shows with
    the parameters, the docstring and the cost, and POST /task/<name>
    for the panel. FastAPI checks the parameters before they spend any
    credit. A task with a wrong cost label fails here, when the app starts.
    """
    parameters_type = parameters_model(name)
    cost = task_cost(task)
    paying = f"Costs {cost} credit{'' if cost == 1 else 's'}, given back if the task fails or is canceled."

    async def start_with_api_key(
        parameters: parameters_type,
        idempotency_key: IdempotencyKey = None,
        account: Account = Depends(get_api_key_account)
            ):
        return await start_task(account, task, parameters.model_dump(mode="json"), idempotency_key)

    # POST because it spends credits: the SameSite=lax cookie still goes
    # with GET navigations from other sites, so a link could trigger it.
    async def start_with_session(
        parameters: parameters_type,
        idempotency_key: IdempotencyKey = None,
        user: OpenID = Depends(get_logged_user)
            ):
        account = await Account.get(google_id=user.id)
        return await start_task(account, task, parameters.model_dump(mode="json"), idempotency_key)

    app.add_api_route(
        f"/api/task/{name}", start_with_api_key, methods=["POST"],
        response_model=TaskInitOut, name=f"start_{name}_with_api_key", tags=["tasks"],
        summary=f"Start {name}",
        description="\n\n".join(filter(None, [inspect.getdoc(task.original_func), paying]))
    )
    app.add_api_route(
        f"/task/{name}", start_with_session, methods=["POST"],
        response_model=TaskInitOut, name=f"start_{name}", include_in_schema=False
    )


for task_name, task in sorted(broker.get_all_tasks().items()):
    add_task_routes(task_name, task)


@app.get("/task/status/{task_id}", response_model=TaskOut)
async def get_status(task_id: str, user: OpenID = Depends(get_logged_user)):
    """Get the status of a specific task."""
    try:
        # Fetch the task from the database
        # and ensure it belongs to the logged-in user
        task_history = await TaskHistory.get(
            task_id=task_id,
            user__google_id=user.id
        )

        # Update the task status and result
        await refresh_task(task_history)

        return TaskOut(id=task_id, status=task_history.status)
    except DoesNotExist:
        raise HTTPException(
            status_code=404,
            detail="Task not found or you do not have permission to access it"
        )
    except Exception as e:
        logger.error("Error retrieving task status %s: %s", task_id, e)
        raise HTTPException(
            status_code=500,
            detail="Error retrieving task status"
        )


async def cancel_task(account: Account, task_id: str) -> TaskOut:
    """
    Cancel the account's task, if no worker started it, giving its credits
    back (see TaskHistory.cancel()). Shared by the routes for the panel and
    for API keys. Canceling it again answers the same, without more credits.
    """
    if await TaskHistory.cancel(task_id, account):
        logger.info("Task %s canceled by its user", task_id)
        return TaskOut(id=task_id, status="CANCELED")

    task = await TaskHistory.get_or_none(task_id=task_id, user_id=account.id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found or you do not have permission to access it"
        )
    if task.status == "CANCELED":
        return TaskOut(id=task_id, status="CANCELED")
    raise HTTPException(
        status_code=409,
        detail="The task already started: only a task that no worker started can be canceled"
    )


@app.post("/api/task/cancel/{task_id}", response_model=TaskOut)
async def cancel_task_with_api_key(task_id: str, account: Account = Depends(get_api_key_account)):
    """
    Cancel a task that no worker started yet, getting its credits back. It
    stays in the history, as CANCELED. One that started can't be canceled.
    """
    return await cancel_task(account, task_id)


# POST, like starting a task: the SameSite=lax cookie still goes with GET
# navigations from other sites, so a link could trigger it.
@app.post("/task/cancel/{task_id}", response_model=TaskOut, include_in_schema=False)
async def cancel_task_with_session(task_id: str, user: OpenID = Depends(get_logged_user)):
    account = await Account.get(google_id=user.id)
    return await cancel_task(account, task_id)


@app.get("/task_details/{task_id}", response_class=HTMLResponse)
async def render_task_details_page(
    task_id: str,
    request: Request,
    user: OpenID = Depends(get_page_user)
        ):
    """
    Render the task details page for the given task ID using a Jinja2 template.
    """
    try:
        # Fetch the task and ensure it belongs to the logged-in user
        task = await TaskHistory.get(
            task_id=task_id,
            user__google_id=user.id
        )

        return templates.TemplateResponse(
            request,
            "task_details.html",
            {
                "task_id": task.task_id,
                "root_prefix": root_prefix
            }
        )

    except DoesNotExist:
        raise HTTPException(
            status_code=404,
            detail="Task not found or you do not have permission to access it"
        )
    except Exception as e:
        logger.error("Error retrieving task details %s: %s", task_id, e)
        raise HTTPException(
            status_code=500,
            detail="Error retrieving task details"
        )


@app.get("/api/task_details_from_user/{task_id}", response_class=JSONResponse)
async def get_task_details_json_from_user(
    task_id: str,
    user: OpenID = Depends(get_logged_user)
        ):
    """
    Fetch task details from the database and return them as JSON.
    """
    try:
        # Fetch the task and ensure it belongs to the logged-in user
        task = await TaskHistory.get(
            task_id=task_id,
            user__google_id=user.id
        )

        # Ensure the task's status and result are up-to-date
        await refresh_task(task)

        # Return task details as JSON
        return JSONResponse(task.details())

    except DoesNotExist:
        raise HTTPException(
            status_code=404,
            detail="Task not found or you do not have permission to access it"
        )
    except Exception as e:
        logger.error("Error retrieving task details %s: %s", task_id, e)
        raise HTTPException(
            status_code=500,
            detail="Error retrieving task details"
        )


@app.get("/api/task_details/{task_id}", response_class=JSONResponse)
async def get_task_details_json(
    task_id: str,
    account: Account = Depends(get_api_key_account)
        ):
    """
    Fetch task details from the database using an API key and return them as JSON.

    Args:
        task_id (str): The ID of the task to retrieve.

    Returns:
        JSONResponse: Task details in JSON format.
    """
    try:
        # Fetch the task and ensure it belongs to the account associated with the API key
        task = await TaskHistory.get_or_none(task_id=task_id, user=account)
        if not task:
            raise HTTPException(
                status_code=404,
                detail="Task not found or you do not have permission to access it"
            )

        # Ensure the task's status and result are up-to-date
        await refresh_task(task)

        # Return task details as JSON
        return JSONResponse(task.details())

    except HTTPException:
        # Let the 404 above through instead of turning it into a 500.
        raise
    except Exception as e:
        logger.error("Error retrieving task details %s: %s", task_id, e)
        raise HTTPException(
            status_code=500,
            detail="Error retrieving task details"
        )


############################
#                          #
#      --- HEALTH ---      #
#                          #
############################
# How long /health waits for PostgreSQL, and for Redis.
HEALTH_TIMEOUT = 2

# The pings that didn't answer in time, until they end: asyncio keeps only
# weak references to its tasks.
unanswered_pings: set[asyncio.Task] = set()


def forget_ping(ping: asyncio.Task) -> None:
    unanswered_pings.discard(ping)
    if not ping.cancelled():
        ping.exception()  # Taken, so asyncio doesn't log it as never retrieved.


async def answers(name: str, ping: typing.Callable[[], typing.Awaitable]) -> bool:
    """Whether a service answers a ping within HEALTH_TIMEOUT seconds: why not goes to the log."""
    task = asyncio.ensure_future(ping())
    await asyncio.wait({task}, timeout=HEALTH_TIMEOUT)
    if not task.done():
        # Canceled, but not awaited: the cleanup of a canceled query, like giving
        # its connection back to the pool, waits for the service too.
        task.cancel()
        unanswered_pings.add(task)
        task.add_done_callback(forget_ping)
        logger.warning("Health check: %s doesn't answer in %d seconds", name, HEALTH_TIMEOUT)
        return False
    if task.exception() is not None:
        logger.warning("Health check: %s doesn't answer: %r", name, task.exception())
        return False
    return True


@app.get("/health", include_in_schema=False)
async def health():
    """
    Whether the app can use PostgreSQL and Redis: 200, or 503 if one of them
    doesn't answer in time, without the details, which go to the log. The
    healthcheck of compose.yaml asks it, and so can an uptime monitor.
    """
    postgres, redis = await asyncio.gather(answers("PostgreSQL", ping_postgres), answers("Redis", ping_redis))
    ok = postgres and redis
    return JSONResponse(
        {
            "status": "ok" if ok else "error",
            "postgres": "ok" if postgres else "error",
            "redis": "ok" if redis else "error",
        },
        status_code=200 if ok else 503
    )


###########################
#                         #
#      --- ADMIN ---      #
#                         #
###########################
# How many accounts the admin page shows, the latest first.
ADMIN_ACCOUNTS_LIMIT = 50


class CreditsIn(BaseModel):
    credits: int = Field(ge=1)


# Out of /docs, which anyone can read: for anyone but ADMINS, they don't exist.
@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request, admin: OpenID = Depends(get_admin)):
    """The admin page: the accounts, with their credits, to give them more."""
    return templates.TemplateResponse(request, "admin.html", {"root_prefix": root_prefix})


@app.get("/admin/accounts", include_in_schema=False)
async def admin_accounts(search: str = "", admin: OpenID = Depends(get_admin)):
    """
    The latest accounts, or the ones with search in their email, with
    their credits and how many of their tasks are pending.
    """
    query = Account.all().order_by("-created_at").limit(ADMIN_ACCOUNTS_LIMIT)
    if search:
        query = query.filter(email__icontains=search)
    accounts = await query
    pending = dict(await TaskHistory.filter(
        user_id__in=[account.id for account in accounts], status="PENDING"
    ).annotate(count=Count("id")).group_by("user_id").values_list("user_id", "count"))
    return [
        {
            "id": account.id,
            "email": account.email,
            "display_name": account.display_name,
            "credits": account.credits,
            "pending_tasks": pending.get(account.id, 0),
            "created_at": json_time(account.created_at),
        }
        for account in accounts
    ]


@app.post("/admin/accounts/{account_id}/credits", include_in_schema=False)
async def admin_add_credits(account_id: int, body: CreditsIn, admin: OpenID = Depends(get_admin)):
    """
    Add credits to an account in a single UPDATE, like scripts/add_credits.py,
    so it can't undo a credit that a task reserves meanwhile. Its credit
    history shows them as the admin's, without saying which admin: the log does.
    """
    balance = await Account.change_credits(account_id, body.credits, "admin")
    if balance is None:
        raise HTTPException(status_code=404, detail="Account not found")
    account = await Account.get(id=account_id)
    logger.info("%s gave %d credits to %s", admin.email, body.credits, account.email)
    return {"credits": balance}


@app.delete("/admin/accounts/{account_id}", include_in_schema=False)
async def admin_delete_account(account_id: int, admin: OpenID = Depends(get_admin)):
    """
    Delete an account, with its tasks, as its user can from the panel (see
    Account.delete_with_tasks()): for a user who asks for it without being
    able to log in, or an account that abuses the service.
    """
    account = await Account.get_or_none(id=account_id)
    if account is None or not await Account.delete_with_tasks(account.google_id):
        raise HTTPException(status_code=404, detail="Account not found")
    # Its ID, and not its email: the account was deleted to forget it.
    logger.info("%s deleted account %d", admin.email, account_id)
    return {"deleted": account_id}


##############################
#                            #
#      --- DATABASE ---      #
#                            #
##############################
async def migrate_database() -> None:
    """
    Apply the pending migrations of web/migrations. A database created
    before the migrations already has the tables of the first one: it's
    only recorded as applied.
    """
    connection = (await Tortoise.init(config=TORTOISE_ORM)).db()
    applied = await MigrationRecorder(connection).applied_migrations()
    [tables] = await connection.execute_query_dict(
        "SELECT to_regclass('account') IS NOT NULL AS created"
    )
    if tables["created"] and not applied:
        logger.info("Recording the existing tables as migration models.0001_initial")
        await migrate(config=TORTOISE_ORM, target="models.0001_initial", fake=True, direction="forward")
    await migrate(config=TORTOISE_ORM, direction="forward")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=51337,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(asctime)s - %(levelname)s - %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S"
                }
            },
            "handlers": {
                "default": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout"
                }
            },
            "loggers": {
                "uvicorn": {
                    "handlers": ["default"],
                    "level": "INFO"
                }
            }
        }
    )
