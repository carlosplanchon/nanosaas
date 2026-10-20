#!/usr/bin/env python3

# to calculate expiration of the JWT
import datetime

import functools

import inspect

import logging

import secrets

import typing

import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Header, HTTPException, Security, Request

from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse

# this is the part that puts the lock icon to the docs
from fastapi.security import APIKeyCookie
from fastapi.security import APIKeyHeader, APIKeyQuery

# pip install fastapi-sso
from fastapi_sso.sso.google import GoogleSSO
from fastapi_sso.sso.base import OpenID

# pip install PyJWT
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

from pydantic import BaseModel, ConfigDict, Field, create_model

from taskiq import AsyncTaskiqDecoratedTask
from taskiq_redis.exceptions import ResultIsMissingError

from taskiq_task import broker
from taskiq_task import ATTEMPTS, LEASE, QUEUE_TIMEOUT, TIME_LIMIT
from taskiq_task import LOG_FORMAT, TaskLost, queue_task

from web.db_models import Account, DeletedAccount, TaskHistory

from shared import CONFIG
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

# The emails of the users who can open the admin page, /admin. Google only
# logs in users with their email verified (fastapi-sso checks it).
ADMINS: set[str] = {email.lower() for email in CONFIG["app"].get("ADMINS", [])}


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


app = FastAPI(
    root_path=root_prefix,
    title="NanoSaaS",
    lifespan=lifespan,
    exception_handlers=tortoise_exception_handlers()
)


# Mount static files directory
app.mount("/web/static", StaticFiles(
        directory="web/static"
    ),
    name="static"
)


# used to sign JWTs, make sure it is really secret.
JWT_SIGNING_SECRET_KEY = CONFIG["api"]["JWT_SIGNING_SECRET_KEY"]
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


def is_admin(user: OpenID) -> bool:
    return (user.email or "").lower() in ADMINS


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
    if user is None or not is_admin(user):
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
    # Google account had one that was deleted (see DeletedAccount).
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


# How many tasks the panel shows, the latest first.
TASK_HISTORY_LIMIT = 50


@app.get("/user/task_history")
async def task_history(user: OpenID = Depends(get_logged_user)):
    """Retrieve the logged user's latest tasks."""
    account = await Account.get(google_id=user.id)
    latest = TaskHistory.filter(user=account).order_by("-created_at").limit(TASK_HISTORY_LIMIT)
    await refresh_tasks(await latest)

    # Refresh history after updates
    history = await latest
    return [
        {
            "id": task.id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "parameters": task.parameters,
            "status": task.status,
            "result": task.result,
            "created_at": task.created_at
        }
        for task in history
    ]


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
            "root_prefix": root_prefix
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
            "is_admin": is_admin(user)
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
    Reserve a credit, save the task in history and queue it in TaskIQ,
    with its parameters and the user's ID. Shared by every route that
    starts a task.

    The credit is taken before queueing, so pending tasks can never
    add up to more than the account's credits. It is given back if
    the task fails, so only successful runs are charged.

    A request with the idempotency key of a task the account already
    started gets that task, without paying again.
    """
    await refresh_overdue_tasks(account.google_id)

    task_id = str(uuid.uuid4())
    # The credit and the row together: every credit taken has a pending
    # row, which gives it back if the task fails or gets lost. The row is
    # saved before the task is queued, so the worker always finds it.
    async with in_transaction():
        if idempotency_key is not None:
            # Lock the account until this ends, so a retry sent meanwhile
            # waits here, and then finds this request's task.
            await Account.select_for_update().get(id=account.id)
            started = await TaskHistory.get_or_none(user=account, idempotency_key=idempotency_key)
            if started is not None:
                if (started.task_type, started.parameters) != (task.task_name, parameters):
                    raise HTTPException(
                        status_code=422,
                        detail="This Idempotency-Key was used for another task or other parameters"
                    )
                return TaskInitOut(id=started.task_id, message="Task started successfully")

        if not await Account.reserve_credit(account.google_id):
            raise HTTPException(status_code=403, detail="Not enough credits")
        await TaskHistory.create(
            user=account,
            task_id=task_id,
            task_type=task.task_name,
            parameters=parameters,
            status="PENDING",
            idempotency_key=idempotency_key
        )

    try:
        await queue_task(task.task_name, task_id, parameters, account.google_id)
    except Exception as e:
        # The error goes to the log: it may show where the servers are.
        logger.error("Task initiation failed for %s: %s", task_id, e.__cause__ or e)

        # Redis may have queued it anyway, and only lost its reply. Give
        # the task up, unless a worker already started it.
        if await TaskHistory.give_up(task_id, account.google_id):
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
    A task that never ends fails at its deadline, giving its credit back.
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
    """The tasks the user can start, with their description and parameters."""
    tasks = []
    for name, task in sorted(broker.get_all_tasks().items()):
        schema = parameters_model(name).model_json_schema()
        definitions = schema.get("$defs", {})
        tasks.append({
            "name": name,
            "description": inspect.getdoc(task.original_func) or "",
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
    the parameters and the docstring, and POST /task/<name> for the
    panel. FastAPI checks the parameters before they spend a credit.
    """
    parameters_type = parameters_model(name)

    async def start_with_api_key(
        parameters: parameters_type,
        idempotency_key: IdempotencyKey = None,
        account: Account = Depends(get_api_key_account)
            ):
        return await start_task(account, task, parameters.model_dump(mode="json"), idempotency_key)

    # POST because it spends a credit: the SameSite=lax cookie still goes
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
        summary=f"Start {name}", description=inspect.getdoc(task.original_func)
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
        return JSONResponse(
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "parameters": task.parameters,
                "status": task.status,
                "result": task.result,
                "created_at": task.created_at.isoformat(),
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
        return JSONResponse(
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "parameters": task.parameters,
                "status": task.status,
                "result": task.result,
                "created_at": task.created_at.isoformat(),
            }
        )

    except HTTPException:
        # Let the 404 above through instead of turning it into a 500.
        raise
    except Exception as e:
        logger.error("Error retrieving task details %s: %s", task_id, e)
        raise HTTPException(
            status_code=500,
            detail="Error retrieving task details"
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
            "created_at": account.created_at,
        }
        for account in accounts
    ]


@app.post("/admin/accounts/{account_id}/credits", include_in_schema=False)
async def admin_add_credits(account_id: int, body: CreditsIn, admin: OpenID = Depends(get_admin)):
    """
    Add credits to an account in a single UPDATE, like scripts/add_credits.py,
    so it can't undo a credit that a task reserves meanwhile.
    """
    if not await Account.filter(id=account_id).update(credits=F("credits") + body.credits):
        raise HTTPException(status_code=404, detail="Account not found")
    account = await Account.get(id=account_id)
    logger.info("%s gave %d credits to %s", admin.email, body.credits, account.email)
    return {"credits": account.credits}


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
