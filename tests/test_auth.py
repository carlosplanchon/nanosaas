"""
Login with Google, the session cookie, the key that signs it, and logout.
Google is stubbed: the tests don't talk to it.
"""
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient
from fastapi_sso.sso.base import OpenID

from tests.helpers import cookie_attributes

SECURE_FLAGS = {"httponly", "secure", "samesite=lax"}

REPO = Path(__file__).resolve().parent.parent
SHORT_KEY = (
    "JWT_SIGNING_SECRET_KEY, in {config}, has {length} characters: it needs at least 32. "
    "Make one with: python3 scripts/generate_jwt_secret_key.py\n"
)


def run_api(config, *arguments):
    """Run Python with these arguments from the repository, with that config file."""
    return subprocess.run(
        [sys.executable, *arguments], cwd=REPO, env={**os.environ, "NANOSAAS_CONFIG": str(config)},
        capture_output=True, text=True, timeout=60,
    )


def test_the_api_doesn_t_start_with_the_template_s_jwt_key():
    # Its key is "...": anyone can read it, and sign a session of any user with it.
    r = run_api("config_template.toml", "-m", "uvicorn", "main_api:app")
    assert r.returncode == 1
    assert r.stderr.endswith(SHORT_KEY.format(config="config_template.toml", length=3)), r.stderr


@pytest.mark.parametrize("length", [31, 32])
def test_the_api_needs_a_jwt_key_of_32_characters(tmp_path, length):
    config = tmp_path / "config.toml"
    template = (REPO / "config_template.toml").read_text()
    config.write_text(template.replace('JWT_SIGNING_SECRET_KEY = "..."', f'JWT_SIGNING_SECRET_KEY = "{"k" * length}"'))
    r = run_api(config, "-c", "import main_api")
    if length < 32:
        assert (r.returncode, r.stderr) == (1, SHORT_KEY.format(config=config, length=length))
    else:
        assert r.returncode == 0, r.stderr


def test_login_redirects_to_google_with_a_state_cookie(app_module, monkeypatch):
    async def discovery_document():
        return {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
        }

    monkeypatch.setattr(app_module.sso, "get_discovery_document", discovery_document)

    with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
        r = client.get("/auth/login", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=test-client-id" in r.headers["location"]
    # fastapi-sso checks this cookie against the state Google sends back.
    assert r.headers["set-cookie"].startswith("sso_state=")
    assert SECURE_FLAGS <= cookie_attributes(r.headers["set-cookie"])


def test_login_creates_the_account_once_and_sets_a_secure_cookie(app_module, services, monkeypatch):
    google_id = f"test-{uuid.uuid4()}"

    async def verify_and_process(request, **kwargs):
        return OpenID(
            id=google_id,
            email=f"{google_id}@example.com",
            first_name="Test",
            last_name="User",
            display_name="Test User",
            picture="https://example.com/picture.png",
            provider="google",
        )

    monkeypatch.setattr(app_module.sso, "verify_and_process", verify_and_process)

    with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
        # The first login creates the account and the second one reuses it.
        for _ in range(2):
            r = client.get(
                "/auth/callback", params={"code": "x", "state": "y"}, follow_redirects=False
            )
            assert (r.status_code, r.headers["location"]) == (307, "/userpanel")
            assert SECURE_FLAGS <= cookie_attributes(r.headers["set-cookie"])
            token = r.cookies["token"]

            # The client won't send a Secure cookie over http: set it by hand.
            client.cookies.set("token", token)
            # INITIAL_CREDITS = 0 in the tests' config: no tasks until someone gives credits.
            assert client.get("/user/credits").json() == {"credits": 0}
            r = client.post("/task/divide", json={"x": 1, "y": 1})
            assert (r.status_code, r.json()) == (403, {"detail": "Not enough credits"})
            assert client.get("/userpanel").status_code == 200

            # Logging out ends the session, so its cookie stops working, and the
            # next login starts another one.
            assert client.post("/auth/logout", follow_redirects=False).status_code == 303
            client.cookies.clear()
            client.cookies.set("token", token)
            assert client.get("/user/credits").status_code == 401
            client.cookies.clear()

    assert asyncio.run(count_accounts(services.dsn, google_id)) == 1


def test_logout_deletes_the_cookie_with_the_same_flags(app_module):
    with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
        r = client.post("/auth/logout", follow_redirects=False)

    assert r.headers["set-cookie"].startswith("token=")
    assert SECURE_FLAGS | {"max-age=0"} <= cookie_attributes(r.headers["set-cookie"])


async def count_accounts(dsn, google_id):
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT count(*) FROM account WHERE google_id = $1", google_id)
    finally:
        await conn.close()
