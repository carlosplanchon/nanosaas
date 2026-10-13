#!/usr/bin/env python3

# to calculate expiration of the JWT
import datetime

import json

import secrets

from fastapi import FastAPI, Depends, HTTPException, Security, Request

from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse

# this is the part that puts the lock icon to the docs
from fastapi.security import APIKeyCookie

# pip install fastapi-sso
from fastapi_sso.sso.google import GoogleSSO
from fastapi_sso.sso.base import OpenID

# pip install python-jose
from jose import jwt

from fastapi.templating import Jinja2Templates

from tortoise.contrib.fastapi import register_tortoise
from tortoise.exceptions import DoesNotExist

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from celery import states
from celery.result import AsyncResult

from celery_task import divide as celery_task_divide

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


google_sso = GoogleSSO(
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI
)

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
    account = await Account.get(google_id=user.id)
    return {"credits": account.credits}


@app.get("/api/user_credits", response_model=dict)
async def get_user_credits_with_key(api_key: str):
    """
    Get the user's remaining credits using an API key.

    Args:
        api_key (str): User's API key for authentication.

    Returns:
        dict: The number of remaining credits.
    """
    account = await Account.get_or_none(api_key=api_key)
    if not account:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return {"credits": account.credits}


@app.get("/user/task_history")
async def task_history(user: OpenID = Depends(get_logged_user)):
    """Retrieve the logged user's task history."""
    account = await Account.get(google_id=user.id)
    history = await TaskHistory.filter(user=account).order_by("-created_at")

    for task_row in history:
        try:
            await refresh_task(task_row)
        except Exception as e:
            print(f"Error updating task {task_row.task_id}: {e}")

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
    Fetch the current API key for the logged-in user.
    """
    account = await Account.get(google_id=user.id)
    return {"api_key": account.api_key}


@app.post("/user/api_key", response_model=dict)
async def create_or_regenerate_api_key(
    user: OpenID = Depends(get_logged_user)
        ):
    account = await Account.get(google_id=user.id)

    # Generate a new API key
    new_api_key = generate_api_key()
    account.api_key = new_api_key
    # Save only this field: a full save would also write back the
    # credits read above, undoing any credit reserved meanwhile.
    await account.save(update_fields=["api_key"])

    return {"api_key": new_api_key}


@app.get("/user/api_key_page", response_class=HTMLResponse)
async def api_key_page(
    request: Request,
    user: OpenID = Depends(get_logged_user)
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
    user: OpenID = Depends(get_logged_user)
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
async def start_divide_task(account: Account, x: int, y: int) -> TaskInitOut:
    """
    Reserve a credit, start the Celery task and save it in history.
    Shared by the cookie and the API key endpoints.

    The credit is taken before queueing, so pending tasks can never
    add up to more than the account's credits. The worker gives it
    back if the task fails, so only successful runs are charged.
    """
    if not await Account.reserve_credit(account.google_id):
        raise HTTPException(status_code=403, detail="Not enough credits")

    task = None
    try:
        # Start the Celery task
        task = celery_task_divide.delay(x, y, account.google_id)

        # Save the task in history
        await TaskHistory.create(
            user=account,
            task_id=task.id,
            task_type="divide",
            parameters={"x": x, "y": y},
            status="PENDING"
        )

        return TaskInitOut(id=task.id, message="Task started successfully")
    except Exception as e:
        if task is None:
            # The task never reached the queue: give the credit back.
            await Account.refund_credit(account.google_id)

        raise HTTPException(
            status_code=500,
            detail=f"Task initiation failed: {e}"
        )


async def refresh_task(task_row: TaskHistory) -> None:
    """
    Update a task's status and result from Celery until it finishes.

    Finished tasks are left alone: Celery forgets results after a day
    and then reports the task as PENDING, which would undo them.
    """
    if task_row.status in states.READY_STATES:
        return

    result = AsyncResult(task_row.task_id)
    task_row.status = result.status

    if not result.ready():
        await task_row.save(update_fields=["status"])
        return

    if result.successful():
        value = result.result
    else:
        # A failed or revoked task's result is an exception. Keep only
        # its type: the message may expose details of the servers.
        value = {
            "status": "error",
            "message": "The task failed",
            "error": type(result.result).__name__,
        }

    # Encode it here: the JSON field can't store the datetimes, Decimals
    # and UUIDs that Celery decodes back into objects, and it takes any
    # str as JSON that is already encoded.
    encoded = json.dumps(value, default=str)
    await TaskHistory.filter(id=task_row.id).update(
        status=task_row.status,
        result=encoded
    )
    task_row.result = json.loads(encoded)


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
async def divide_with_api_key(x: int, y: int, api_key: str):
    """
    Endpoint for initiating a division task using an API key.

    Args:
        x (int): Dividend.
        y (int): Divisor.
        api_key (str): User's API key for authentication.

    Returns:
        TaskInitOut: Details about the initiated task.
    """
    # Validate API key
    account = await Account.get_or_none(api_key=api_key)
    if not account:
        raise HTTPException(status_code=401, detail="Invalid API key")

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
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving task status: {e}"
        )


@app.get("/task_details/{task_id}", response_class=HTMLResponse)
async def render_task_details_page(
    task_id: str,
    request: Request,
    user: OpenID = Depends(get_logged_user)
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
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving task details: {e}"
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
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving task details: {e}"
        )


@app.get("/api/task_details/{task_id}", response_class=JSONResponse)
async def get_task_details_json(task_id: str, api_key: str):
    """
    Fetch task details from the database using an API key and return them as JSON.

    Args:
        task_id (str): The ID of the task to retrieve.
        api_key (str): User's API key for authentication.

    Returns:
        JSONResponse: Task details in JSON format.
    """
    try:
        # Validate API key
        account = await Account.get_or_none(api_key=api_key)
        if not account:
            raise HTTPException(status_code=401, detail="Invalid API key")

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
        # Let the 401/404 above through instead of turning them into a 500.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving task details: {e}"
        )


##############################
#                            #
#      --- DATABASE ---      #
#                            #
##############################
# Register Tortoise with SQLite
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
