"""
The security headers: every response has them, with a Content-Security-Policy
that lets the pages load only this server's files and blocks inline scripts.
The pages of the docs, which FastAPI makes, go without the policy.
"""
import re
from pathlib import Path

import pytest

from tests.helpers import client_for, insert_task

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent

HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
}
# Only this server's files, without inline scripts nor eval, and in no frame.
POLICY = {
    "default-src": ["'self'"],
    "script-src": ["'self'"],
    "object-src": ["'none'"],
    "base-uri": ["'none'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
}


def headers(response):
    return {name: response.headers.get(name) for name in HEADERS}


def policy(response):
    """The directives of the response's Content-Security-Policy, or None without one."""
    header = response.headers.get("content-security-policy")
    if header is None:
        return None
    return {name: values for name, *values in (directive.split() for directive in header.split(";"))}


async def test_every_response_has_the_security_headers(db, account, user, anon):
    task_id = await insert_task(db, account, status="SUCCESS", result=2.0)
    responses = [await user.get(path) for path in (
        # The pages, and what they load and ask for.
        "/", "/userpanel", f"/task_details/{task_id}", "/user/api_key_page", "/thankyou",
        "/web/static/user_panel.js", "/user/task_history",
    )]
    responses += [await anon.get(path, headers=key) for path, key in [
        # The API, its errors and its redirects.
        ("/api/user_credits", {"X-API-Key": account.api_key}),
        ("/api/user_credits", {}),
        ("/no-such-page", {}),
        ("/userpanel", {}),
        ("/openapi.json", {}),
    ]]
    assert [r.status_code for r in responses] == [200] * 8 + [401, 404, 307, 200]
    for r in responses:
        assert (headers(r), policy(r)) == (HEADERS, POLICY), r.url


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
async def test_the_docs_go_without_the_policy(anon, path):
    # FastAPI makes them with inline scripts, which load Swagger UI and ReDoc from a CDN.
    r = await anon.get(path)
    assert r.status_code == 200
    assert (headers(r), policy(r)) == (HEADERS, None)


async def test_with_a_root_path_too(prefixed_server, account):
    async with client_for(prefixed_server, account.cookie) as client:
        panel = await client.get("/nanosaas/userpanel")
        docs = await client.get("/nanosaas/docs")
    assert (policy(panel), policy(docs)) == (POLICY, None)
    # Its script takes the URLs' ROOT_PATH from the page.
    assert '<html lang="en" data-root-prefix="/nanosaas">' in panel.text
    assert 'src="/nanosaas/web/static/user_panel.js?v=' in panel.text


def test_the_templates_have_no_inline_scripts_or_styles():
    # The policy blocks them: the scripts are files in web/static, and the
    # attributes of Alpine.js, or the scripts' listeners, handle the events.
    blocked = {
        "inline script": r"<script(?![^>]*\ssrc=)[^>]*>",
        "event handler attribute": r"\son[a-z]+\s*=",
        "javascript: URL": r"javascript:",
        "style element or attribute": r"<style|\sstyle\s*=",
    }
    for template in (REPO / "web" / "templates").glob("*.html"):
        text = template.read_text()
        for name, pattern in blocked.items():
            assert not re.search(pattern, text), f"{name} in {template.name}"


def test_the_alpine_expressions_need_no_eval():
    # The pages load Alpine.js's CSP build, which reads the expressions of its
    # attributes itself: they can use only the component's data and methods, and
    # it fails on the rest, in the browser. That code goes in the pages' scripts.
    unsupported = {
        "arrow function": r"=>",
        "template literal": r"`",
        "optional chaining": r"\?\.",
        "nullish coalescing": r"\?\?",
        "new": r"\bnew\s",
        "global": r"\b(JSON|Math|Date|Object|window|document|console|confirm|alert|encodeURIComponent)\b",
    }
    for template in (REPO / "web" / "templates").glob("*.html"):
        text = template.read_text()
        assert "x-html" not in text, f"x-html, which the CSP build doesn't have, in {template.name}"
        if "x-data" in text:
            assert "alpine-csp-" in text, f"{template.name} doesn't load the CSP build"
        for expression in re.findall(r'\s(?:x-[\w.:-]+|:[\w.-]+|@[\w.:-]+)="([^"]*)"', text):
            for name, pattern in unsupported.items():
                assert not re.search(pattern, expression), f"{name} in {template.name}: {expression}"
