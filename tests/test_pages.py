"""Pages, static files, and the redirect to the login page without a valid session."""
import datetime
import re
from pathlib import Path

import pytest

from tests.helpers import client_for, insert_task, session_cookie

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
EXPIRED = datetime.timedelta(minutes=-1)


async def test_public_pages_and_static_files(anon):
    r = await anon.get("/")
    assert r.status_code == 200
    assert "Login with Google" in r.text
    assert (await anon.get("/thankyou")).status_code == 200
    # With an empty ROOT_PATH, FastAPI's root_path used to end up as "/"
    # and the static files gave 404.
    assert (await anon.get("/web/static/nanosaas_logo.svg")).status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/userpanel", "/user/api_key_page", "/user/credits", "/user/task_history", "/user/api_key"],
)
async def test_user_pages_with_a_session(user, path):
    assert (await user.get(path)).status_code == 200


@pytest.mark.parametrize("path", ["/", "/thankyou", "/userpanel", "/user/api_key_page"])
async def test_pages_have_a_favicon_and_the_built_css(user, path):
    page = await user.get(path)
    assert "cdn.tailwindcss.com" not in page.text
    for rel, content_type in [("icon", "image/svg+xml"), ("stylesheet", "text/css")]:
        href = re.search(rf'<link rel="{rel}"[^>]* href="(/[^"]+)"', page.text).group(1)
        r = await user.get(href)
        assert (r.status_code, r.headers["content-type"].split(";")[0]) == (200, content_type), rel


def test_the_built_css_has_every_class_of_the_templates_and_scripts():
    css = (REPO / "web" / "static" / "tailwind.css").read_text()
    # The scripts write class attributes too, like json.js for the colors of the results.
    files = [*(REPO / "web" / "templates").glob("*.html"), *(REPO / "web" / "static").glob("*.js")]
    for file in files:
        for classes in re.findall(r'class="([^"]*)"', file.read_text()):
            for name in classes.split():
                # Tailwind escapes these characters in its selectors.
                selector = "." + re.sub(r"([:/.\[\]])", r"\\\1", name)
                assert selector in css, f"{name} in {file.name}: run scripts/build_css.py"


def test_the_templates_static_files_exist():
    for template in (REPO / "web" / "templates").glob("*.html"):
        for path in re.findall(r'"\{\{ root_prefix \}\}/web/static/([^"]+)"', template.read_text()):
            assert (REPO / "web" / "static" / path).is_file(), f"{path} in {template.name}"


def test_scripts_and_styles_from_other_sites_have_an_integrity_hash():
    for template in (REPO / "web" / "templates").glob("*.html"):
        for tag in re.findall(r'<(?:script|link)\b[^>]*"https://[^>]*>', template.read_text()):
            assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag, f"{template.name}: {tag}"


async def test_credits_with_the_api_key(account, anon):
    r = await anon.get("/api/user_credits", params={"api_key": account.api_key})
    assert r.json() == {"credits": 5}


async def test_logout_takes_a_post_and_redirects_to_the_thank_you_page(user):
    # The panel's button posts to it: a link from another site can't log the user out.
    assert '<form method="post" action="/auth/logout">' in (await user.get("/userpanel")).text
    assert (await user.get("/auth/logout")).status_code == 405
    r = await user.post("/auth/logout")
    assert (r.status_code, r.headers["location"]) == (303, "/thankyou")


async def test_logging_out_ends_every_session_of_the_account(server, account):
    # Sessions on two devices: one from before session versions, and one from after.
    sessions = [account.cookie, session_cookie(account.google_id, version=0)]
    async with client_for(server, sessions[0]) as user:
        assert (await user.post("/auth/logout")).status_code == 303

    for cookie in sessions:
        async with client_for(server, cookie) as client:
            assert (await client.get("/user/credits")).status_code == 401
            assert (await client.get("/userpanel")).headers["location"] == "/"
    # The next login's session has the account's new version.
    async with client_for(server, session_cookie(account.google_id, version=1)) as client:
        assert (await client.get("/user/credits")).status_code == 200


@pytest.mark.parametrize("session", ["missing", "expired", "wrong signature"])
async def test_pages_redirect_to_login_without_a_valid_session(server, db, account, session):
    task_id = await insert_task(db, account, status="SUCCESS", result=2.0)
    cookie = {
        "missing": None,
        "expired": session_cookie(account.google_id, expires_in=EXPIRED),
        "wrong signature": session_cookie(account.google_id, key="another-secret-of-at-least-32-bytes"),
    }[session]

    async with client_for(server, cookie) as client:
        for page in ("/userpanel", "/user/api_key_page", f"/task_details/{task_id}"):
            r = await client.get(page)
            assert (r.status_code, r.headers.get("location")) == (307, "/"), page


async def test_json_routes_still_answer_401_without_a_valid_session(server, db, account):
    task_id = await insert_task(db, account, status="SUCCESS", result=2.0)
    expired = session_cookie(account.google_id, expires_in=EXPIRED)

    async with client_for(server, expired) as client:
        for path in (
            "/user/credits",
            "/user/task_history",
            f"/task/status/{task_id}",
            f"/api/task_details_from_user/{task_id}",
        ):
            r = await client.get(path)
            assert r.status_code == 401, path
            assert r.headers["content-type"] == "application/json", path


async def test_pages_and_links_with_a_root_path(prefixed_server):
    async with client_for(prefixed_server) as client:
        r = await client.get("/nanosaas/userpanel")
        assert (r.status_code, r.headers.get("location")) == (307, "/nanosaas/")
        r = await client.get("/nanosaas/")
        assert 'href="/nanosaas/auth/login"' in r.text
        assert 'href="/nanosaas/web/static/nanosaas_logo.svg"' in r.text
        assert 'href="/nanosaas/web/static/tailwind.css"' in r.text
        assert '<img src="/nanosaas/web/static/nanosaas_logo.svg"' in r.text
        assert (await client.get("/nanosaas/web/static/nanosaas_logo.svg")).status_code == 200
