"""
Login with Google, the session cookie and logout. Google is stubbed: the
tests don't talk to it.
"""
import asyncio
import uuid

import asyncpg
from fastapi.testclient import TestClient
from fastapi_sso.sso.base import OpenID

from tests.helpers import cookie_attributes

SECURE_FLAGS = {"httponly", "secure", "samesite=lax"}


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

            # The client won't send a Secure cookie over http: set it by hand.
            client.cookies.set("token", r.cookies["token"])
            # INITIAL_CREDITS = 0 in the tests' config: no tasks until someone gives credits.
            assert client.get("/user/credits").json() == {"credits": 0}
            r = client.post("/task/divide", json={"x": 1, "y": 1})
            assert (r.status_code, r.json()) == (403, {"detail": "Not enough credits"})
            assert client.get("/userpanel").status_code == 200
            client.cookies.clear()

    assert asyncio.run(count_accounts(services.dsn, google_id)) == 1


def test_logout_deletes_the_cookie_with_the_same_flags(app_module):
    with TestClient(app_module.app, base_url="http://127.0.0.1") as client:
        r = client.get("/auth/logout", follow_redirects=False)

    assert r.headers["set-cookie"].startswith("token=")
    assert SECURE_FLAGS | {"max-age=0"} <= cookie_attributes(r.headers["set-cookie"])


async def count_accounts(dsn, google_id):
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT count(*) FROM account WHERE google_id = $1", google_id)
    finally:
        await conn.close()
