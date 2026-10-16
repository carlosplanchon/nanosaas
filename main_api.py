#!/usr/bin/env python3

# to calculate expiration of the JWT
import datetime

import functools

import inspect

import secrets

import time

import typing

import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Security, Request

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
from tortoise.migrations.api import migrate
from tortoise.migrations.recorder import MigrationRecorder
from tortoise.transactions import in_transaction

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, ConfigDict, create_model

from taskiq import AsyncTaskiqDecoratedTask
from taskiq_redis.exceptions import ResultIsMissingError

from taskiq_task import broker
from taskiq_task import divide as taskiq_task_divide
from taskiq_task import QUEUE_TIMEOUT, TIME_LIMIT

from web.db_models import Account, TaskHistory

from shared import CONFIG
from shared import TORTOISE_ORM


#############################
#                           #
#      --- FASTAPI ---      #
#                           #
#############################
templates = Jinja2Templates(directory="web/templates")


root_path: str = CONFIG["app"]["ROOT_PATH"]

if root_path != "":
    root_prefix = f"/{root_path}"
else:
    root_prefix = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        return OpenID(**claims["pld"])
    except Exception as error:
        print("Error.")
        print(error)
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials"
        ) from error


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


@app.get("/auth/login")
async def login():
    """Redirect the user to the Google login page."""
    async with sso:
        return await sso.get_login_redirect()


@app.get("/auth/logout", response_class=HTMLResponse)
async def logout(request: Request):
    """Forget the user's session and return an HTML response."""
    response = RedirectResponse(url=f"{root_prefix}/thankyou")
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

    # Check if account exists, if not, create it
    account, created = await Account.get_or_create(
        google_id=openid.id,
        defaults={
            "email": openid.email,
            "first_name": openid.first_name,
            "last_name": openid.last_name,
            "display_name": openid.display_name,
            "picture": openid.picture,
            "provider": openid.provider,
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
            "sub": openid.id
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
            "root_prefix": root_prefix
        }
    )


###########################
#                         #
#      --- TASKS ---      #
#                         #
###########################
# Past this, a pending task was lost: TaskIQ can't start or run it anymore.
TASK_DEADLINE = datetime.timedelta(seconds=QUEUE_TIMEOUT + TIME_LIMIT + 60)


class TaskLost(Exception):
    """A task that didn't end by its deadline: its worker died, or the broker lost it."""


@functools.cache
def parameters_model(task_name: str) -> type[BaseModel]:
    """
    A model of the task's parameters, from its function's signature and
    type hints: every parameter except user_id, which start_task() adds.
    """
    function = broker.find_task(task_name).original_func
    hints = typing.get_type_hints(function)
    fields = {
        name: (
            hints.get(name, typing.Any),
            ... if parameter.default is inspect.Parameter.empty else parameter.default
        )
        for name, parameter in inspect.signature(function).parameters.items()
        if name != "user_id"
    }
    return create_model(
        f"{task_name}_parameters", __config__=ConfigDict(extra="forbid"), **fields
    )


async def start_task(account: Account, task: AsyncTaskiqDecoratedTask, parameters: dict) -> TaskInitOut:
    """
    Reserve a credit, save the task in history and queue it in TaskIQ,
    with its parameters and the user's ID. Shared by every route that
    starts a task.

    The credit is taken before queueing, so pending tasks can never
    add up to more than the account's credits. It is given back if
    the task fails, so only successful runs are charged.
    """
    await refresh_overdue_tasks(account.google_id)

    task_id = str(uuid.uuid4())
    # The credit and the row together: every credit taken has a pending
    # row, which gives it back if the task fails or gets lost. The row is
    # saved before the task is queued, so the worker always finds it.
    async with in_transaction():
        if not await Account.reserve_credit(account.google_id):
            raise HTTPException(status_code=403, detail="Not enough credits")
        await TaskHistory.create(
            user=account,
            task_id=task_id,
            task_type=task.task_name,
            parameters=parameters,
            status="PENDING"
        )

    try:
        # Queue the TaskIQ task
        await task.kicker().with_task_id(task_id).with_labels(
            timeout=TIME_LIMIT,
            queue_deadline=time.time() + QUEUE_TIMEOUT
        ).kiq(**parameters, user_id=account.google_id)
    except Exception as e:
        # The error goes to the log: it may show where the servers are.
        print(f"Task initiation failed for {task_id}: {e.__cause__ or e}")

        # Redis may have queued it anyway, and only lost its reply. Give
        # the task up, unless a worker already started it.
        if await TaskHistory.give_up(task_id, account.google_id):
            raise HTTPException(
                status_code=500,
                detail="Task initiation failed"
            )
        print(f"Task {task_id} was queued after all: a worker started it.")

    return TaskInitOut(id=task_id, message="Task started successfully")


async def refresh_task(task_row: TaskHistory) -> None:
    """
    Finish a pending task from TaskIQ's result backend.

    The worker saves each task when it ends. This covers the ones it
    couldn't save (for example, with the database down), as long as the
    result backend still has their result: it forgets results after a day.
    A task that never ends fails at TASK_DEADLINE, giving its credit back.
    """
    if task_row.status != "PENDING":
        return

    try:
        result = await broker.result_backend.get_result(task_row.task_id)
    except ResultIsMissingError:
        if task_row.created_at < datetime.datetime.now(tz=datetime.timezone.utc) - TASK_DEADLINE:
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
            print(f"Error updating task {task_row.task_id}: {e}")


async def refresh_overdue_tasks(google_id: str) -> None:
    """Refresh the account's pending tasks past TASK_DEADLINE, so the credits are right."""
    created_before = datetime.datetime.now(tz=datetime.timezone.utc) - TASK_DEADLINE
    overdue = await TaskHistory.filter(
        user__google_id=google_id, created_at__lt=created_before, status="PENDING"
    )
    await refresh_tasks(overdue)


@app.get("/user/tasks")
async def list_tasks(user: OpenID = Depends(get_logged_user)):
    """The tasks the user can start, with their description and parameters."""
    tasks = []
    for name, task in sorted(broker.get_all_tasks().items()):
        properties = parameters_model(name).model_json_schema().get("properties", {})
        tasks.append({
            "name": name,
            "description": inspect.getdoc(task.original_func) or "",
            "parameters": [
                {"name": parameter, "type": schema.get("type", "string"), "default": schema.get("default")}
                for parameter, schema in properties.items()
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
        account: Account = Depends(get_api_key_account)
            ):
        return await start_task(account, task, parameters.model_dump(mode="json"))

    # POST because it spends a credit: the SameSite=lax cookie still goes
    # with GET navigations from other sites, so a link could trigger it.
    async def start_with_session(
        parameters: parameters_type,
        user: OpenID = Depends(get_logged_user)
            ):
        account = await Account.get(google_id=user.id)
        return await start_task(account, task, parameters.model_dump(mode="json"))

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


# divide's own routes, still here for the clients that use them.
@app.post("/task/divide/{x}/{y}", response_model=TaskInitOut)
async def divide(
    x: int,
    y: int,
    user: OpenID = Depends(get_logged_user)
        ):
    account = await Account.get(google_id=user.id)
    return await start_task(account, taskiq_task_divide, {"x": x, "y": y})


@app.get("/api/task/divide/{x}/{y}", response_model=TaskInitOut)
async def divide_with_api_key(
    x: int,
    y: int,
    account: Account = Depends(get_api_key_account)
        ):
    """
    Endpoint for initiating a division task using an API key.

    Args:
        x (int): Dividend.
        y (int): Divisor.

    Returns:
        TaskInitOut: Details about the initiated task.
    """
    return await start_task(account, taskiq_task_divide, {"x": x, "y": y})


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
        print(f"Error retrieving task status {task_id}: {e}")
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
        print(f"Error retrieving task details {task_id}: {e}")
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
        print(f"Error retrieving task details {task_id}: {e}")
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
        print(f"Error retrieving task details {task_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error retrieving task details"
        )


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
        print("Recording the existing tables as migration models.0001_initial")
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
