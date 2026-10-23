"""
Webhooks (webhooks.py): when each of an account's tasks ends, its webhook's
URL gets a POST with the task, signed as Standard Webhooks says, which these
tests check as receivers would, with its library, standardwebhooks.
"""
import asyncio
import base64
import copy
import datetime
import json
import uuid

import asyncpg
import httpx
import pytest
from standardwebhooks.webhooks import Webhook as Verifier
from tortoise import Tortoise

from tests.helpers import Receiver, client_for, create_account, insert_task, task_row, wait_for_lock, wait_until_done

pytestmark = pytest.mark.anyio

NO_WEBHOOK = {"url": None, "secret": None, "disabled_at": None, "disabled_reason": None, "failed": 0, "deliveries": []}
# How many attempts a delivery gets: RETRIES in webhooks.py.
ATTEMPTS = 10
SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
# Why the worker disables a webhook, after a 410.
GONE = "Its URL answered 410 Gone: it wants no more events."
# What the user panel says while the account's webhook is disabled.
DISABLED_NOTICE = "Your webhook is disabled"


@pytest.fixture
def receiver():
    receiver = Receiver()
    yield receiver
    receiver.stop()


@pytest.fixture
async def own_database(app_module, services):
    """
    A database of its own, which no worker reads, migrated, with Tortoise
    set up on it in this process: the app's module, to use its models.
    """
    from tortoise.migrations.api import migrate

    name = f"own_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(services.dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    config = copy.deepcopy(app_module.TORTOISE_ORM)
    config["connections"]["default"] = config["connections"]["default"].rsplit("/", 1)[0] + "/" + name
    await Tortoise.init(config=config)
    try:
        await migrate(config=config, direction="forward")
        yield app_module
    finally:
        await Tortoise.close_connections()


async def own_webhook(app):
    """An account, with a webhook that no worker sends to, in own_database."""
    account = await app.Account.create(
        google_id=f"own-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", picture="", provider="google"
    )
    return account, await app.Webhook.create(account=account, url="https://example.com/", secret=SECRET)


async def set_webhook(server, account, url):
    """Set the account's webhook from the API key page: the webhook, as the page gets it."""
    async with client_for(server, account.cookie) as user:
        r = await user.put("/user/webhook", json={"url": url})
        assert r.status_code == 200, r.text
        return r.json()


async def send_test_event(server, account):
    """Queue a test event from the API key page: its delivery, as the page shows it."""
    async with client_for(server, account.cookie) as user:
        r = await user.post("/user/webhook/test")
        assert r.status_code == 200, r.text
        return r.json()["deliveries"][0]


def verified(received, secret):
    """The event of a request, checked as its receiver would: it raises if the signature is wrong."""
    return Verifier(secret).verify(received.body, received.headers)


async def delivery(db, message_id):
    return await db.fetchrow("SELECT * FROM webhookdelivery WHERE message_id = $1", message_id)


async def wait_for_delivery(db, message_id, until, timeout=30):
    """Wait until a delivery's row passes `until`, and return it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not until(row := await delivery(db, message_id)):
        assert asyncio.get_running_loop().time() < deadline, dict(row)
        await asyncio.sleep(0.2)
    return row


def status_is(status):
    return lambda row: row["status"] == status


def attempted(row):
    return row["last_response"] is not None


async def insert_delivery(db, account, attempts=0):
    """A test event for the account's webhook, due now, after `attempts` attempts: its ID."""
    message_id = f"msg_{uuid.uuid4().hex}"
    await db.execute(
        """
        INSERT INTO webhookdelivery (webhook_id, message_id, type, payload, created_at, status, attempts, next_attempt_at)
        SELECT id, $2, 'webhook.test', '{}', now(), 'PENDING', $3, now() FROM webhook WHERE account_id = $1
        """,
        account.id, message_id, attempts,
    )
    return message_id


async def webhook_row(db, account):
    return await db.fetchrow("SELECT * FROM webhook WHERE account_id = $1", account.id)


async def wait_for_webhook(db, account, until, timeout=30):
    """Wait until the row of the account's webhook passes `until`, and return it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not until(row := await webhook_row(db, account)):
        assert asyncio.get_running_loop().time() < deadline, dict(row)
        await asyncio.sleep(0.2)
    return row


def disabled(row):
    return row["disabled_at"] is not None


async def test_an_account_sets_its_webhook_from_the_api_key_page(webhook_server, webhook_account, receiver):
    async with client_for(webhook_server, webhook_account.cookie) as user:
        assert (await user.get("/user/webhook")).json() == NO_WEBHOOK

        webhook = (await user.put("/user/webhook", json={"url": receiver.url})).json()
        assert (webhook["url"], webhook["deliveries"]) == (receiver.url, [])
        # "whsec_" and the base64 of 32 random bytes.
        assert webhook["secret"].startswith("whsec_")
        assert len(base64.b64decode(webhook["secret"].removeprefix("whsec_"))) == 32
        assert (await user.get("/user/webhook")).json() == webhook

        # Another URL keeps the secret, until it's replaced.
        other = (await user.put("/user/webhook", json={"url": receiver.url + "/other"})).json()
        assert (other["url"], other["secret"]) == (receiver.url + "/other", webhook["secret"])
        replaced = (await user.post("/user/webhook/secret")).json()
        assert (replaced["url"], replaced["secret"] != webhook["secret"]) == (receiver.url + "/other", True)

        assert (await user.delete("/user/webhook")).json() == NO_WEBHOOK
        assert (await user.get("/user/webhook")).json() == NO_WEBHOOK
        for path in ("/user/webhook/secret", "/user/webhook/test", "/user/webhook/enable", "/user/webhook/resend"):
            r = await user.post(path)
            assert (r.status_code, r.json()) == (404, {"detail": "Set a webhook URL first."})

        # The page has the webhook's inputs and buttons, which its script fills and shows.
        page = (await user.get("/user/api_key_page")).text
        for element in ("webhook_url", "webhook_secret", "webhook-enable", "webhook-resend"):
            assert f'id="{element}"' in page


async def test_the_webhook_takes_the_session_only(anon, account):
    requests = [
        ("GET", "/user/webhook"), ("PUT", "/user/webhook"), ("DELETE", "/user/webhook"),
        ("POST", "/user/webhook/secret"), ("POST", "/user/webhook/test"),
        ("POST", "/user/webhook/enable"), ("POST", "/user/webhook/resend"),
    ]
    for method, path in requests:
        # Not even with an API key: a leaked one can't send the account's results somewhere else.
        r = await anon.request(
            method, path, json={"url": "https://example.com/hook"}, headers={"X-API-Key": account.api_key}
        )
        assert r.status_code == 401, (method, path)
    # Nor in /docs, which anyone can read.
    assert not [path for path in (await anon.get("/openapi.json")).json()["paths"] if "webhook" in path]


@pytest.mark.parametrize("url, error", [
    ("http://example.com/hook", "The URL must start with https://."),
    ("example.com/hook", "The URL must start with https://."),
    ("https://user:password@example.com/hook", "The URL can't have a user or a password."),
    ("https:///hook", "The URL has no host."),
    ("https://example.com:0/hook", "The URL's port isn't valid."),
    ("https://" + "a" * 2000 + ".com/", "The URL is longer than 2000 characters."),
    ("https://nowhere.invalid/hook", "Can't find the address of nowhere.invalid."),
    ("https://127.0.0.1/hook", "127.0.0.1 has a private address."),
    ("https://10.0.0.1/hook", "10.0.0.1 has a private address."),
    ("https://169.254.169.254/latest/meta-data", "169.254.169.254 has a private address."),
    ("https://224.0.0.1/hook", "224.0.0.1 has a private address."),
    ("https://[::1]/hook", "::1 has a private address."),
    # IPv6 addresses for 127.0.0.1: mapped, and through NAT64.
    ("https://[::ffff:127.0.0.1]/hook", "::ffff:127.0.0.1 has a private address."),
    ("https://[64:ff9b::7f00:1]/hook", "64:ff9b::7f00:1 has a private address."),
    ("https://localhost/hook", "localhost has a private address."),
])
async def test_a_webhook_url_is_https_to_a_public_address(user, url, error):
    # The tests' server takes neither http:// nor private addresses, as on a server.
    r = await user.put("/user/webhook", json={"url": url})
    assert (r.status_code, r.json()) == (422, {"detail": error})
    assert (await user.get("/user/webhook")).json() == NO_WEBHOOK


async def test_a_public_https_url_is_taken(user):
    # An address, so the test doesn't need the internet's DNS. Its account ends no task.
    r = await user.put("/user/webhook", json={"url": "https://1.1.1.1/hook"})
    assert (r.status_code, r.json()["url"]) == (200, "https://1.1.1.1/hook")


async def test_each_task_that_ends_sends_its_event_signed(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    secret = (await set_webhook(webhook_server, webhook_account, receiver.url))["secret"]
    async with client_for(webhook_server, webhook_account.cookie) as user:
        succeeded = (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]
        failed = (await user.post("/task/divide", json={"x": 1, "y": 0})).json()["id"]
        events = {}
        for received in await receiver.wait_for(2):
            assert received.path == receiver.path
            assert received.headers["content-type"] == "application/json"
            assert received.headers["user-agent"] == "NanoSaaS-Webhooks"
            event = verified(received, secret)
            assert event["type"] == "task.ended"
            assert datetime.datetime.fromisoformat(event["timestamp"]).utcoffset() == datetime.timedelta(0)
            events[event["data"]["task_id"]] = event["data"]
        # The task as its account gets it from the API.
        for task_id in (succeeded, failed):
            assert events[task_id] == (await user.get(f"/api/task_details_from_user/{task_id}")).json()

        # The page shows them, delivered at the first attempt.
        for received in receiver.received:
            await wait_for_delivery(webhook_db, received.headers["webhook-id"], status_is("DELIVERED"))
        deliveries = (await user.get("/user/webhook")).json()["deliveries"]
    assert {(d["task_id"], d["type"], d["status"], d["attempts"], d["last_response"]) for d in deliveries} == {
        (succeeded, "task.ended", "DELIVERED", 1, "200 OK"), (failed, "task.ended", "DELIVERED", 1, "200 OK"),
    }
    assert (events[succeeded]["status"], events[succeeded]["result"]) == ("SUCCESS", 2.0)
    assert events[failed]["status"] == "FAILURE"
    assert events[failed]["result"] == {"status": "error", "message": "Can't divide by zero.", "error": "TaskError"}


async def test_a_canceled_task_sends_its_event(webhook_server, webhook_worker, webhook_account, webhook_db, receiver):
    secret = (await set_webhook(webhook_server, webhook_account, receiver.url))["secret"]
    # As start_task() leaves it: no worker took it yet.
    task_id = await insert_task(webhook_db, webhook_account)
    async with client_for(webhook_server, webhook_account.cookie) as user:
        assert (await user.post(f"/task/cancel/{task_id}")).json()["status"] == "CANCELED"
        [received] = await receiver.wait_for(1)
        data = verified(received, secret)["data"]
        assert data == (await user.get(f"/api/task_details_from_user/{task_id}")).json()
    assert (data["task_id"], data["status"], data["result"]) == (task_id, "CANCELED", None)


async def test_the_test_event(webhook_server, webhook_worker, webhook_account, receiver):
    secret = (await set_webhook(webhook_server, webhook_account, receiver.url))["secret"]
    queued = await send_test_event(webhook_server, webhook_account)
    assert (queued["type"], queued["task_id"], queued["status"]) == ("webhook.test", None, "PENDING")
    [received] = await receiver.wait_for(1)
    assert received.headers["webhook-id"] == queued["id"]
    event = verified(received, secret)
    assert (event["type"], event["data"]) == ("webhook.test", {})

    # Not right away again: users could send any number of requests anywhere.
    async with client_for(webhook_server, webhook_account.cookie) as user:
        r = await user.post("/user/webhook/test")
    assert (r.status_code, r.json()) == (429, {"detail": "Wait 10 seconds between test events."})


async def test_a_failed_attempt_is_tried_again_with_the_same_id(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    secret = (await set_webhook(webhook_server, webhook_account, receiver.url))["secret"]
    receiver.statuses = [500]
    await send_test_event(webhook_server, webhook_account)
    first, second = await receiver.wait_for(2)

    # 5 seconds later, plus up to a tenth: the same event, signed again at its time.
    assert 5 <= second.time - first.time < 10
    assert first.headers["webhook-id"] == second.headers["webhook-id"]
    assert first.body == second.body
    assert int(first.headers["webhook-timestamp"]) < int(second.headers["webhook-timestamp"])
    assert verified(first, secret) == verified(second, secret)
    row = await wait_for_delivery(webhook_db, first.headers["webhook-id"], status_is("DELIVERED"))
    assert (row["attempts"], row["last_response"]) == (2, "200 OK")


async def test_a_pending_delivery_goes_to_the_new_url_with_the_new_secret(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    receiver.statuses = [500]
    queued = await send_test_event(webhook_server, webhook_account)
    await wait_for_delivery(webhook_db, queued["id"], attempted)

    other = Receiver()
    try:
        await set_webhook(webhook_server, webhook_account, other.url)
        async with client_for(webhook_server, webhook_account.cookie) as user:
            secret = (await user.post("/user/webhook/secret")).json()["secret"]
        # Its next attempt, now.
        await webhook_db.execute(
            "UPDATE webhookdelivery SET next_attempt_at = now() WHERE message_id = $1", queued["id"]
        )
        [received] = await other.wait_for(1)
        assert received.headers["webhook-id"] == queued["id"]
        assert verified(received, secret)["type"] == "webhook.test"
    finally:
        other.stop()
    assert len(receiver.received) == 1


async def test_a_delivery_fails_after_its_last_attempt(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    receiver.statuses = [503]
    # Its last attempt is due.
    message_id = await insert_delivery(webhook_db, webhook_account, attempts=ATTEMPTS - 1)
    row = await wait_for_delivery(webhook_db, message_id, status_is("FAILED"))
    assert (row["attempts"], row["last_response"]) == (ATTEMPTS, "503 Service Unavailable")
    assert len(receiver.received) == 1


async def test_a_410_stops_the_attempts_and_disables_the_webhook(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    receiver.statuses = [410]
    queued = await send_test_event(webhook_server, webhook_account)
    row = await wait_for_delivery(webhook_db, queued["id"], status_is("FAILED"))
    assert (row["attempts"], row["last_response"]) == (1, "410 Gone")
    # The receiver wants no more events: as Standard Webhooks says, the worker stops sending them.
    await wait_for_webhook(webhook_db, webhook_account, disabled)
    async with client_for(webhook_server, webhook_account.cookie) as user:
        webhook = (await user.get("/user/webhook")).json()
    assert (webhook["disabled_reason"], webhook["failed"]) == (GONE, 1)
    assert datetime.datetime.fromisoformat(webhook["disabled_at"]) <= datetime.datetime.now(tz=datetime.timezone.utc)
    log = webhook_worker.log.read_text()
    assert f"The webhook of account {webhook_account.id}, to 127.0.0.1, was disabled: {GONE}" in log
    # The next attempt would come 5 seconds later.
    await asyncio.sleep(7)
    assert len(receiver.received) == 1


async def test_attempts_that_fail_for_3_days_disable_the_webhook(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    # Like the last attempt of an event whose attempts all failed, the first one 3 days ago.
    await webhook_db.execute(
        "UPDATE webhook SET failing_since = now() - interval '3 days' WHERE account_id = $1", webhook_account.id
    )
    receiver.statuses = [503]
    message_id = await insert_delivery(webhook_db, webhook_account, attempts=ATTEMPTS - 1)
    await wait_for_delivery(webhook_db, message_id, status_is("FAILED"))
    row = await wait_for_webhook(webhook_db, webhook_account, disabled)
    assert row["disabled_reason"] == "Its attempts failed for 3 days in a row. The last one: 503 Service Unavailable."


async def test_a_delivered_event_ends_the_failures(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    # Failing for almost 3 days: one more failure doesn't disable it yet.
    await webhook_db.execute(
        "UPDATE webhook SET failing_since = now() - interval '3 days' + interval '1 hour' WHERE account_id = $1",
        webhook_account.id,
    )
    receiver.statuses = [500]
    queued = await send_test_event(webhook_server, webhook_account)
    await wait_for_delivery(webhook_db, queued["id"], attempted)
    # Its next attempt, 5 seconds later, delivers it.
    await wait_for_delivery(webhook_db, queued["id"], status_is("DELIVERED"))
    row = await wait_for_webhook(webhook_db, webhook_account, lambda row: row["failing_since"] is None)
    assert row["disabled_at"] is None


async def test_a_disabled_webhook_s_events_wait_until_it_s_enabled(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    await webhook_db.execute(
        "UPDATE webhook SET failing_since = now() - interval '3 days', disabled_at = now(), disabled_reason = $2 "
        "WHERE account_id = $1",
        webhook_account.id, GONE,
    )
    async with client_for(webhook_server, webhook_account.cookie) as user:
        # The panel says so, and the API key page why.
        assert DISABLED_NOTICE in (await user.get("/userpanel")).text
        task_id = (await user.post("/task/divide", json={"x": 6, "y": 3})).json()["id"]
        await wait_until_done(user, task_id)
        # Its task's event waits.
        await asyncio.sleep(3)
        [delivery] = (await user.get("/user/webhook")).json()["deliveries"]
        assert (delivery["task_id"], delivery["status"], delivery["attempts"]) == (task_id, "PENDING", 0)
        assert receiver.received == []
        # Test events would wait too, and so would the failed ones sent again: the page sends neither.
        for path in ("/user/webhook/test", "/user/webhook/resend"):
            r = await user.post(path)
            assert (r.status_code, r.json()) == (409, {"detail": "Enable the webhook first."})

        receiver.statuses = [500]
        enabled = (await user.post("/user/webhook/enable")).json()
        assert (enabled["disabled_at"], enabled["disabled_reason"]) == (None, None)
        # The event goes out. Its failures start again: one more doesn't disable it again,
        # and the next attempt, 5 seconds later, delivers it.
        first, second = await receiver.wait_for(2)
        assert first.headers["webhook-id"] == second.headers["webhook-id"] == delivery["id"]
        assert DISABLED_NOTICE not in (await user.get("/userpanel")).text


async def test_saving_the_url_enables_the_webhook_again(webhook_server, webhook_account, webhook_db, receiver):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    await webhook_db.execute(
        "UPDATE webhook SET failing_since = now(), disabled_at = now(), disabled_reason = $2 WHERE account_id = $1",
        webhook_account.id, GONE,
    )
    # The same URL, or another one: its failures are forgotten too.
    webhook = await set_webhook(webhook_server, webhook_account, receiver.url)
    assert (webhook["disabled_at"], webhook["disabled_reason"]) == (None, None)
    assert (await webhook_row(webhook_db, webhook_account))["failing_since"] is None


async def test_the_failed_events_are_sent_again_with_their_ids(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    # Two events that failed their last attempts, and one delivered.
    receiver.statuses = [503, 503]
    failed = [await insert_delivery(webhook_db, webhook_account, attempts=ATTEMPTS - 1) for _ in range(2)]
    for message_id in failed:
        await wait_for_delivery(webhook_db, message_id, status_is("FAILED"))
    delivered = await insert_delivery(webhook_db, webhook_account)
    await wait_for_delivery(webhook_db, delivered, status_is("DELIVERED"))
    receiver.received.clear()

    async with client_for(webhook_server, webhook_account.cookie) as user:
        assert (await user.get("/user/webhook")).json()["failed"] == 2
        webhook = (await user.post("/user/webhook/resend")).json()
    # As new, with their IDs: receivers skip the ones they got.
    assert webhook["failed"] == 0
    resent = {(d["id"], d["status"], d["attempts"], d["last_response"]) for d in webhook["deliveries"] if d["id"] in failed}
    assert resent == {(message_id, "PENDING", 0, None) for message_id in failed}
    received = await receiver.wait_for(2)
    assert sorted(r.headers["webhook-id"] for r in received) == sorted(failed)
    for message_id in failed:
        row = await wait_for_delivery(webhook_db, message_id, status_is("DELIVERED"))
        assert row["attempts"] == 1
    # Not the delivered one.
    await asyncio.sleep(2)
    assert len(receiver.received) == 2


async def test_a_redirect_is_not_followed(webhook_server, webhook_worker, webhook_account, webhook_db, receiver):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    # Somewhere the worker didn't check: it's a failed attempt, and the next one goes to the URL again.
    receiver.statuses = [(307, {"Location": "/redirected"})]
    queued = await send_test_event(webhook_server, webhook_account)
    row = await wait_for_delivery(webhook_db, queued["id"], status_is("DELIVERED"))
    assert row["attempts"] == 2
    assert [received.path for received in receiver.received] == [receiver.path] * 2


async def test_a_webhook_goes_to_its_hosts_name(webhook_server, webhook_worker, webhook_account, receiver):
    # The worker sends it to an address it checked, with the host's name, as virtual hosts need.
    # localhost may have ::1 first, where nothing listens: then it tries 127.0.0.1.
    url = f"http://localhost:{receiver.port}{receiver.path}"
    secret = (await set_webhook(webhook_server, webhook_account, url))["secret"]
    await send_test_event(webhook_server, webhook_account)
    [received] = await receiver.wait_for(1)
    assert received.headers["host"] == f"localhost:{receiver.port}"
    assert verified(received, secret)["type"] == "webhook.test"


async def test_the_log_has_the_host_but_not_the_url(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    # A URL with a token in it, which shouldn't end up in the logs.
    token = uuid.uuid4().hex
    await set_webhook(webhook_server, webhook_account, f"{receiver.url}?token={token}")
    receiver.statuses = [500]
    queued = await send_test_event(webhook_server, webhook_account)
    await receiver.wait_for(2)
    await wait_for_delivery(webhook_db, queued["id"], status_is("DELIVERED"))
    log = webhook_worker.log.read_text()
    assert f"Webhook delivery {queued['id']} to 127.0.0.1 failed, attempt 1 of {ATTEMPTS}: 500" in log
    assert token not in log


async def test_no_cookie_goes_from_a_webhook_to_another(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    # A receiver that sets a cookie, at the same address as another account's.
    receiver.statuses = [(200, {"Set-Cookie": "session=secret"})]
    await set_webhook(webhook_server, webhook_account, receiver.url)
    await send_test_event(webhook_server, webhook_account)
    await receiver.wait_for(1)

    other = Receiver()
    try:
        account = await create_account(webhook_db)
        await set_webhook(webhook_server, account, other.url)
        await send_test_event(webhook_server, account)
        [received] = await other.wait_for(1)
        assert "cookie" not in received.headers
    finally:
        other.stop()


async def test_a_delivery_that_cant_be_signed_fails_its_attempts(
    webhook_server, webhook_worker, webhook_account, webhook_db, receiver
):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    # A secret that isn't base64: the attempts fail, as when the receiver doesn't answer,
    # instead of the delivery being claimed again and again, past its last attempt.
    await webhook_db.execute("UPDATE webhook SET secret = 'whsec_!' WHERE account_id = $1", webhook_account.id)
    queued = await send_test_event(webhook_server, webhook_account)
    row = await wait_for_delivery(webhook_db, queued["id"], attempted)
    assert (row["status"], row["attempts"]) == ("PENDING", 1)
    assert row["last_response"] == "Error: the event couldn't be sent"
    assert row["next_attempt_at"] > datetime.datetime.now(tz=datetime.timezone.utc)
    assert receiver.received == []
    # Its next attempts would fail too.
    await webhook_db.execute("DELETE FROM webhook WHERE account_id = $1", webhook_account.id)


async def test_a_webhook_to_a_private_address_is_not_sent(worker, db, account, user, receiver):
    # Saved straight in the database, past the checks of the API: the worker checks the address
    # again before each attempt, since the host's addresses can change.
    await db.execute(
        "INSERT INTO webhook (account_id, url, secret, created_at) VALUES ($1, $2, $3, now())",
        account.id, f"https://127.0.0.1:{receiver.port}{receiver.path}", SECRET,
    )
    queued = (await user.post("/user/webhook/test")).json()["deliveries"][0]
    row = await wait_for_delivery(db, queued["id"], attempted)
    assert (row["status"], row["attempts"], row["last_response"]) == ("PENDING", 1, "127.0.0.1 has a private address")
    assert receiver.received == []
    # Its next attempts would be blocked too.
    await db.execute("DELETE FROM webhook WHERE account_id = $1", account.id)


async def test_deleting_the_account_deletes_its_webhook(webhook_server, webhook_account, webhook_db, receiver):
    await set_webhook(webhook_server, webhook_account, receiver.url)
    webhook_id = await webhook_db.fetchval("SELECT id FROM webhook WHERE account_id = $1", webhook_account.id)
    async with client_for(webhook_server, webhook_account.cookie) as user:
        await user.post("/user/webhook/test")
        assert (await user.post("/user/delete_account")).status_code == 303
    assert await webhook_db.fetchval("SELECT count(*) FROM webhook WHERE id = $1", webhook_id) == 0
    assert await webhook_db.fetchval("SELECT count(*) FROM webhookdelivery WHERE webhook_id = $1", webhook_id) == 0


async def test_deleting_an_account_with_a_webhook_waits_for_a_task_that_is_ending(services, db, account, user):
    # Never due: no worker sends it.
    webhook_id = await db.fetchval(
        "INSERT INTO webhook (account_id, url, secret, created_at) VALUES ($1, 'https://example.com/', $2, now()) "
        "RETURNING id",
        account.id, SECRET,
    )
    task_id = await insert_task(db, account)
    ending = await asyncpg.connect(services.dsn)
    try:
        async with ending.transaction():
            # Like TaskHistory.finish() of a failed task: it locks the task's row, records the event,
            # which locks the webhook's row for its key...
            await ending.execute("UPDATE taskhistory SET status = 'FAILURE' WHERE task_id = $1", task_id)
            await ending.execute(
                "INSERT INTO webhookdelivery (webhook_id, message_id, type, payload, created_at, status, attempts, "
                "next_attempt_at) VALUES ($1, $2, 'task.ended', '{}', now(), 'PENDING', 0, now() + interval '1 day')",
                webhook_id, f"msg_{uuid.uuid4().hex}",
            )
            deleting = asyncio.create_task(user.post("/user/delete_account"))
            await wait_for_lock(db, 'DELETE FROM "taskhistory"')
            # ...and then the account's, to give the credit back. The deletion waits for the
            # task's row before it deletes the account, and the webhook with it: no deadlock.
            await asyncio.wait_for(
                ending.execute("UPDATE account SET credits = credits + 1 WHERE id = $1", account.id), 5
            )
        assert (await deleting).status_code == 303
    finally:
        await ending.close()
    assert await db.fetchval("SELECT count(*) FROM webhook WHERE id = $1", webhook_id) == 0


async def finish_in_process(app_module, task_id):
    """TaskHistory.finish() of a task that returned 2.0, in this process."""
    await Tortoise.init(config=app_module.TORTOISE_ORM)
    try:
        return await app_module.TaskHistory.finish(task_id, 2.0)
    finally:
        await Tortoise.close_connections()


async def test_a_task_ends_while_its_webhook_is_removed(app_module, services, db, account):
    webhook_id = await db.fetchval(
        "INSERT INTO webhook (account_id, url, secret, created_at) VALUES ($1, 'https://example.com/', $2, now()) "
        "RETURNING id",
        account.id, SECRET,
    )
    task_id = await insert_task(db, account)
    removing = await asyncpg.connect(services.dsn)
    try:
        transaction = removing.transaction()
        await transaction.start()
        await removing.execute("DELETE FROM webhook WHERE id = $1", webhook_id)
        # The event's insert waits for the webhook's removal...
        finishing = asyncio.create_task(finish_in_process(app_module, task_id))
        await wait_for_lock(db, 'INSERT INTO "webhookdelivery"')
        await transaction.commit()
        # ...and then fails, without undoing the end of the task.
        assert await asyncio.wait_for(finishing, 10) is True
    finally:
        await removing.close()
    assert await task_row(db, task_id) == ("SUCCESS", 2.0)
    assert await db.fetchval("SELECT count(*) FROM webhookdelivery WHERE webhook_id = $1", webhook_id) == 0


async def test_a_task_that_ends_once_records_one_event(own_database):
    app = own_database
    account, webhook = await own_webhook(app)
    task = await app.TaskHistory.create(user=account, task_id=str(uuid.uuid4()), task_type="divide", parameters={})
    # Like the worker and the API's requests, which may all end it at once: only one does.
    ended = await asyncio.gather(*(app.TaskHistory.finish(task.task_id, 2.0) for _ in range(10)))
    assert sorted(ended) == [False] * 9 + [True]
    assert await app.WebhookDelivery.filter(webhook_id=webhook.id).count() == 1


async def test_two_workers_claim_each_delivery_once(own_database):
    from webhooks import claim_due

    app = own_database
    _, webhook = await own_webhook(app)
    for _ in range(10):
        await app.WebhookDelivery.queue(webhook, "webhook.test", {})

    # Two workers looking for them at once: both find the 10, and each one claims some.
    first, second = await asyncio.gather(claim_due(), claim_due())
    claimed = [delivery.message_id for delivery in first + second]
    assert len(claimed) == len(set(claimed)) == 10
    assert [delivery.attempts for delivery in await app.WebhookDelivery.all()] == [1] * 10


async def test_the_cleanup_deletes_the_deliveries_that_ended_a_week_ago(own_database):
    from webhooks import cleanup

    app = own_database
    _, webhook = await own_webhook(app)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    for status, days in [("DELIVERED", 1), ("DELIVERED", 8), ("FAILED", 1), ("FAILED", 8), ("PENDING", 8)]:
        await app.WebhookDelivery.queue(webhook, "webhook.test", {})
        await app.WebhookDelivery.filter(last_attempt_at__isnull=True).update(
            status=status, last_attempt_at=now - datetime.timedelta(days=days)
        )
    await cleanup()
    kept = {(delivery.status, (now - delivery.last_attempt_at).days) for delivery in await app.WebhookDelivery.all()}
    assert kept == {("DELIVERED", 1), ("FAILED", 1), ("PENDING", 8)}


async def test_the_cleanup_deletes_the_events_that_waited_a_week_for_their_webhook(own_database):
    from webhooks import cleanup

    app = own_database
    _, working = await own_webhook(app)
    _, off = await own_webhook(app)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    await app.Webhook.filter(id=off.id).update(disabled_at=now - datetime.timedelta(days=8))
    for webhook in (working, off):
        for days in (1, 8):
            await app.WebhookDelivery.queue(webhook, "webhook.test", {})
            delivery = await app.WebhookDelivery.filter(webhook_id=webhook.id).order_by("-id").first()
            await app.WebhookDelivery.filter(id=delivery.id).update(created_at=now - datetime.timedelta(days=days))
    await cleanup()
    # An enabled webhook's pending ones are being sent.
    kept = {(delivery.webhook_id, (now - delivery.created_at).days) for delivery in await app.WebhookDelivery.all()}
    assert kept == {(working.id, 1), (working.id, 8), (off.id, 1)}


async def test_the_workers_skip_the_disabled_webhooks(own_database):
    from webhooks import claim_due

    app = own_database
    _, working = await own_webhook(app)
    _, off = await own_webhook(app)
    await app.Webhook.filter(id=off.id).update(disabled_at=datetime.datetime.now(tz=datetime.timezone.utc))
    for webhook in (working, off):
        await app.WebhookDelivery.queue(webhook, "webhook.test", {})
    assert [delivery.webhook_id for delivery in await claim_due()] == [working.id]


async def webhook_after(app, webhook, status, answer, failing_since=None, url=None):
    """The webhook after an attempt with that status and answer, as track() leaves it."""
    from webhooks import track

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    await app.Webhook.filter(id=webhook.id).update(failing_since=failing_since and now - failing_since)
    if url is not None:
        # Its URL changed while the attempt went to the one before.
        await app.Webhook.filter(id=webhook.id).update(url=url)
    await track(webhook, status, answer, now)
    return await app.Webhook.get(id=webhook.id), now


@pytest.mark.parametrize("failing_for, disables", [
    (datetime.timedelta(days=3) - datetime.timedelta(minutes=1), False), (datetime.timedelta(days=3), True),
])
async def test_a_webhook_is_disabled_after_3_days_of_failed_attempts(own_database, failing_for, disables):
    app = own_database
    _, webhook = await own_webhook(app)
    row, now = await webhook_after(app, webhook, 500, "500 Internal Server Error", failing_since=failing_for)
    assert (row.disabled_at, row.disabled_reason) == (
        (now, "Its attempts failed for 3 days in a row. The last one: 500 Internal Server Error.") if disables
        else (None, None)
    )


async def test_the_first_failed_attempt_starts_the_failures_and_a_delivered_one_ends_them(own_database):
    app = own_database
    _, webhook = await own_webhook(app)
    for status, answer in [(None, "Timeout"), (404, "404 Not Found"), (500, "500 Internal Server Error")]:
        row, now = await webhook_after(app, webhook, status, answer)
        assert (row.failing_since, row.disabled_at) == (now, None)
    # A later failure keeps when they started.
    row, _ = await webhook_after(app, webhook, 500, "500 Internal Server Error", failing_since=datetime.timedelta(hours=1))
    assert datetime.timedelta(hours=1) <= datetime.datetime.now(tz=datetime.timezone.utc) - row.failing_since
    for status in (200, 204, 299):
        row, _ = await webhook_after(app, webhook, status, f"{status} OK", failing_since=datetime.timedelta(days=2))
        assert row.failing_since is None


async def test_an_attempt_to_the_url_before_doesn_t_change_the_webhook(own_database):
    app = own_database
    _, webhook = await own_webhook(app)
    row, _ = await webhook_after(app, webhook, 410, "410 Gone", url="https://example.org/")
    assert (row.failing_since, row.disabled_at) == (None, None)
    row, _ = await webhook_after(
        app, webhook, 200, "200 OK", failing_since=datetime.timedelta(days=1), url="https://example.net/"
    )
    assert row.failing_since is not None


@pytest.mark.parametrize("address, public", [
    ("8.8.8.8", True), ("2606:4700::1111", True), ("64:ff9b::808:808", True),
    ("127.0.0.1", False), ("0.0.0.0", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("::1", False), ("fe80::1", False), ("fc00::1", False),
    # Mapped, through NAT64, compatible and translated: IPv4 addresses inside IPv6 ones.
    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False), ("64:ff9b::7f00:1", False), ("64:ff9b::a00:1", False),
    ("::127.0.0.1", False), ("::ffff:0:a00:1", False),
    # Multicast, and IPv6's deprecated site-local addresses, which Python takes as global.
    ("224.0.0.1", False), ("239.255.255.250", False), ("ff02::1", False), ("ff0e::1", False), ("fec0::1", False),
])
def test_only_public_addresses_get_webhooks(app_module, address, public):
    from webhooks import is_public

    assert is_public(address) is public


async def test_a_host_with_a_private_address_among_others_gets_no_webhooks(app_module, monkeypatch):
    import webhooks

    async def getaddrinfo(host, port, **kwargs):
        return [(0, 0, 0, "", ("8.8.8.8", port)), (0, 0, 0, "", ("10.0.0.1", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    with pytest.raises(webhooks.Undeliverable, match="mixed.example has a private address"):
        await webhooks.resolve("mixed.example", 443)


async def test_a_slow_receiver_runs_out_of_time(app_module, monkeypatch, receiver):
    import webhooks

    # It would answer, but too late.
    monkeypatch.setattr(webhooks, "ALLOW_PRIVATE", True)
    monkeypatch.setattr(webhooks, "DEADLINE", 1)
    receiver.delay = 3
    start = asyncio.get_running_loop().time()
    async with webhooks.client() as client:
        assert await webhooks.send(client, receiver.url, SECRET, "msg_slow", "{}") == (None, "Timeout")
    assert asyncio.get_running_loop().time() - start < 2


def test_the_signature_is_standard_webhooks(app_module):
    from webhooks import new_secret, sign

    secret = new_secret()
    body = json.dumps({"type": "webhook.test", "timestamp": "2026-10-21T12:00:00+00:00", "data": {}}).encode()
    timestamp = int(datetime.datetime.now().timestamp())
    signature = sign(secret, "msg_test", timestamp, body)
    headers = {"webhook-id": "msg_test", "webhook-timestamp": str(timestamp), "webhook-signature": signature}
    assert Verifier(secret).verify(body, headers)["type"] == "webhook.test"
    with pytest.raises(Exception, match="No matching signature"):
        Verifier(new_secret()).verify(body, headers)


def test_the_client_keeps_no_cookies(app_module):
    from webhooks import client

    request = httpx.Request("POST", "http://1.1.1.1/hook")
    response = httpx.Response(200, headers={"Set-Cookie": "session=secret"}, request=request)
    cookies = client().cookies
    cookies.extract_cookies(response)
    assert len(cookies.jar) == 0
