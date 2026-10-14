#!/usr/bin/env python3

# to calculate expiration of the JWT
import datetime

import secrets

import uuid

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

from tortoise.contrib.fastapi import register_tortoise
from tortoise.exceptions import DoesNotExist

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from celery import states
from celery.result import AsyncResult

from celery_task import divide as celery_task_divide
from celery_task import QUEUE_TIMEOUT, TIME_LIMIT

from web.db_models import Account, TaskHistory

from shared import CONFIG
from shared import DB_URL


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


app = FastAPI(
    root_path=root_prefix,
    title="NanoSaaS"
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
# Response model for Celery task status.
class TaskOut(BaseModel):
    id: str
    status: str


# Response model for Celery task initiation.
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


@app.get("/user/task_history")
async def task_history(user: OpenID = Depends(get_logged_user)):
    """Retrieve the logged user's task history."""
    account = await Account.get(google_id=user.id)
    history = await TaskHistory.filter(user=account).order_by("-created_at")
    await refresh_tasks(history)

    # Refresh history after updates
    history = await TaskHistory.filter(user=account).order_by("-created_at")
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
#      --- CELERY ---      #
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
# Past this, an unfinished task was lost: Celery can't start or run it anymore.
TASK_DEADLINE = datetime.timedelta(seconds=QUEUE_TIMEOUT + TIME_LIMIT + 60)


class TaskLost(Exception):
    """A task that didn't end by its deadline: its worker died, or the broker lost it."""


async def start_divide_task(account: Account, x: int, y: int) -> TaskInitOut:
    """
    Reserve a credit, save the task in history and start it in Celery.
    Shared by the cookie and the API key endpoints.

    The credit is taken before queueing, so pending tasks can never
    add up to more than the account's credits. It is given back if
    the task fails, so only successful runs are charged.
    """
    await refresh_overdue_tasks(account.google_id)
    if not await Account.reserve_credit(account.google_id):
        raise HTTPException(status_code=403, detail="Not enough credits")

    task_id = str(uuid.uuid4())
    try:
        # Save the task in history before queueing it, so the worker
        # always finds its row when it saves the result.
        await TaskHistory.create(
            user=account,
            task_id=task_id,
            task_type="divide",
            parameters={"x": x, "y": y},
            status="PENDING"
        )

        # Start the Celery task
        celery_task_divide.apply_async(
            (x, y, account.google_id),
            task_id=task_id,
            expires=QUEUE_TIMEOUT
        )
    except Exception as e:
        # The task never reached the queue: drop its row and give the
        # credit back.
        await TaskHistory.filter(task_id=task_id).delete()
        await Account.refund_credit(account.google_id)

        # The error goes to the log: it may show where the servers are.
        print(f"Task initiation failed for {task_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Task initiation failed"
        )

    return TaskInitOut(id=task_id, message="Task started successfully")


async def refresh_task(task_row: TaskHistory) -> None:
    """
    Update an unfinished task from Celery.

    The worker saves each task when it ends. This covers the ones it
    couldn't save (for example, with the database down), as long as
    Celery still has their result: it forgets results after a day.
    A task that never ends fails at TASK_DEADLINE, giving its credit back.
    """
    if task_row.status in states.READY_STATES:
        return

    result = AsyncResult(task_row.task_id)
    # Read it once: the task may end between two reads.
    state = result.state

    if state in states.READY_STATES:
        await TaskHistory.finish(task_row.task_id, state, result.result)
    elif task_row.created_at < datetime.datetime.now(tz=datetime.timezone.utc) - TASK_DEADLINE:
        await TaskHistory.finish(task_row.task_id, states.FAILURE, TaskLost())
    else:
        # Only while unfinished: the worker may have just saved it.
        await TaskHistory.filter(id=task_row.id).exclude(
            status__in=states.READY_STATES
        ).update(status=state)

    await task_row.refresh_from_db(fields=["status", "result"])


async def refresh_tasks(task_rows: list[TaskHistory]) -> None:
    """refresh_task() for each row, skipping the ones that fail."""
    for task_row in task_rows:
        try:
            await refresh_task(task_row)
        except Exception as e:
            print(f"Error updating task {task_row.task_id}: {e}")


async def refresh_overdue_tasks(google_id: str) -> None:
    """Refresh the account's unfinished tasks past TASK_DEADLINE, so the credits are right."""
    created_before = datetime.datetime.now(tz=datetime.timezone.utc) - TASK_DEADLINE
    overdue = await TaskHistory.filter(
        user__google_id=google_id, created_at__lt=created_before
    ).exclude(status__in=states.READY_STATES)
    await refresh_tasks(overdue)


# POST because it spends a credit: the SameSite=lax cookie still goes
# with GET navigations from other sites, so a link could trigger it.
@app.post("/task/divide/{x}/{y}", response_model=TaskInitOut)
async def divide(
    x: int,
    y: int,
    user: OpenID = Depends(get_logged_user)
        ):
    account = await Account.get(google_id=user.id)
    return await start_divide_task(account, x, y)


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
    return await start_divide_task(account, x, y)


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
# Register Tortoise with PostgreSQL
register_tortoise(
    app,
    db_url=DB_URL,
    modules={"models": ["web.db_models"]},
    generate_schemas=True,
    add_exception_handlers=True
)


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
