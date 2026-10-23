#!/usr/bin/env python3
"""
Webhooks: an account with one gets a POST at its URL when each of its tasks
ends, with the task as /api/task_details gives it. They follow Standard
Webhooks (https://www.standardwebhooks.com/), so receivers can check them
with its libraries:

- The body is {"type": "task.ended", "timestamp": ..., "data": <the task>}, or
  a "webhook.test" event from the API key page, with empty data.
- webhook-id identifies the event, the same in each attempt: receivers skip
  the ones they got. webhook-timestamp is when the attempt was sent, and
  webhook-signature is "v1," and the base64 of the HMAC-SHA256 of
  "<id>.<timestamp>.<body>", with the webhook's secret.
- Only a 2xx answer delivers it. Others, timeouts and errors get more
  attempts, for about 3 days (RETRIES), except a 410.
- As Standard Webhooks recommends, a receiver under load, which answers
  429, 502 or 504, gets the webhook's events more slowly: they wait
  THROTTLE before the next one goes. An answer with a Retry-After header,
  like a 503's, makes them wait as long as it asks, up to MAX_WAIT, and its
  event's next attempt too, if later than its schedule's.
- As Standard Webhooks recommends, the worker disables a webhook whose URL
  answers 410, which says it wants no more events, or whose attempts keep
  failing for DISABLE_AFTER, without one that delivers its event: the
  workers stop trying a URL that's gone. Its events wait, unsent, for up to
  KEEP, until its account enables it again, or saves its URL. The pages say
  so, and the API key page sends the failed events again.

TaskHistory.finish() and cancel() record the events (WebhookDelivery), and
each worker process sends them (deliver_webhooks()). They go only to public
addresses, checked when they're sent, not only when the URL is saved: other
ones would let users reach the server's internal services.
"""
import asyncio
import base64
import datetime
import email.utils
import hashlib
import hmac
import http.cookiejar
import ipaddress
import logging
import math
import random
import secrets
import socket
import time

import httpx
from tortoise import timezone
from tortoise.expressions import F, Q, Subquery

from shared import CONFIG
from web.db_models import Webhook, WebhookDelivery

logger = logging.getLogger(__name__)
# httpx logs each request with its whole URL, which may have a token in it.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Lets webhooks go to http:// URLs and private addresses, like localhost: only
# to try them on your machine.
ALLOW_PRIVATE = CONFIG["app"].get("ALLOW_PRIVATE_WEBHOOKS", False)

MAX_URL_LENGTH = 2000
DEFAULT_PORTS = {"https": 443, "http": 80}

# Seconds before each attempt of a delivery, as in Standard Webhooks' example:
# 10 attempts in about 3 days. Each delay gets up to a tenth more, at random, so
# the retries of many deliveries don't all come at once.
RETRIES = [0, 5, 5 * 60, 30 * 60, 2 * 3600, 5 * 3600, 10 * 3600, 14 * 3600, 20 * 3600, 24 * 3600]
# The answers of a receiver under load, as Standard Webhooks says, after which
# the webhook's events wait THROTTLE: unless the answer says how long with
# Retry-After, which any answer can have. Up to MAX_WAIT, the longest step of RETRIES.
UNDER_LOAD = {429, 502, 504}
THROTTLE = datetime.timedelta(minutes=1)
MAX_WAIT = datetime.timedelta(days=1)
# A webhook whose attempts keep failing for this long gets disabled: a bit less
# than the attempts of an event, so one that fails all of them disables it.
DISABLE_AFTER = datetime.timedelta(days=3)
# A worker claims each delivery it sends for this long: if it dies meanwhile, another one sends it then.
CLAIM = datetime.timedelta(seconds=60)
# Seconds a receiver has to connect, and for each read and write, and for the
# whole attempt: well within CLAIM, so no other worker sends it meanwhile.
TIMEOUT = 10
DEADLINE = 30
# Each worker process sends up to BATCH deliveries at once, each on its own, so a
# slow receiver holds up only its own, and looks for more every POLL seconds.
BATCH = 20
POLL = 1
# How long the page shows a delivery after its last attempt, and how long one
# waits while its webhook is disabled.
KEEP = datetime.timedelta(days=7)

# The IPv6 addresses of NAT64, which reach the IPv4 address in their last 32 bits.
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# IPv6 addresses that Python takes as global, but aren't on the internet: the
# deprecated site-local ones, and IPv4 addresses written as IPv6 ones, the
# compatible and the translated ones.
NOT_GLOBAL = [ipaddress.ip_network(network) for network in ("fec0::/10", "::/96", "::ffff:0:0:0/96")]


class Undeliverable(Exception):
    """A webhook's URL that can't get its events, and why."""


def new_secret() -> str:
    """A secret to sign a webhook's events: "whsec_" and the base64 of 32 random bytes."""
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """The webhook-signature header of an attempt."""
    key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    digest = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def is_public(address: str) -> bool:
    """
    Whether an IP address is on the internet: not this machine's, a private
    network's, a multicast group's, nor reserved. An IPv6 address with an IPv4
    one inside counts as that one: some versions of Python take the mapped
    ones as global, and all of them take the NAT64 ones.
    """
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif any(ip in network for network in NOT_GLOBAL):
            return False
    return ip.is_global and not ip.is_multicast


async def resolve(host: str, port: int) -> list[str]:
    """
    The addresses to send a webhook to: those of its host, which must all be
    public, unless ALLOW_PRIVATE_WEBHOOKS. Raises Undeliverable otherwise.
    """
    try:
        found = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), TIMEOUT
        )
    except (OSError, UnicodeError):
        # OSError covers wait_for's TimeoutError too.
        raise Undeliverable(f"Can't find the address of {host}") from None
    addresses = list(dict.fromkeys(info[4][0] for info in found))
    if not ALLOW_PRIVATE and not all(is_public(address) for address in addresses):
        raise Undeliverable(f"{host} has a private address")
    return addresses


def parse(url: str) -> tuple[httpx.URL, str, int]:
    """
    A webhook's URL as it's sent to, with its host and its port. Raises
    Undeliverable if it can't be sent to.
    """
    if len(url) > MAX_URL_LENGTH:
        raise Undeliverable(f"The URL is longer than {MAX_URL_LENGTH} characters")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise Undeliverable("The URL isn't valid") from None
    if parsed.scheme not in (DEFAULT_PORTS if ALLOW_PRIVATE else ["https"]):
        raise Undeliverable("The URL must start with https://" + (" or http://" if ALLOW_PRIVATE else ""))
    if not parsed.raw_host:
        raise Undeliverable("The URL has no host")
    port = DEFAULT_PORTS[parsed.scheme] if parsed.port is None else parsed.port
    if not 0 < port < 65536:
        raise Undeliverable("The URL's port isn't valid")
    if parsed.userinfo:
        raise Undeliverable("The URL can't have a user or a password")
    return parsed, parsed.raw_host.decode(), port


async def check_url(url: str) -> None:
    """
    Check a webhook's URL before it's saved, as it's checked before each
    attempt. Raises Undeliverable with what's wrong.
    """
    _, host, port = parse(url)
    await resolve(host, port)


def host_of(url: str) -> str:
    """A webhook's host, for the log: the rest of its URL may have a token in it."""
    try:
        return httpx.URL(url).host
    except httpx.InvalidURL:
        return "an invalid URL"


def client() -> httpx.AsyncClient:
    """The HTTP client of a worker process, which sends its webhooks."""
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        # Not to where redirects go, which wasn't checked.
        follow_redirects=False,
        # Without the proxies of the environment, which would skip the check of the addresses.
        trust_env=False,
        # A connection for each attempt: one kept for the same address may have checked
        # another host's certificate.
        limits=httpx.Limits(max_keepalive_connections=0),
        # Without keeping cookies: the next requests to the same address would take them,
        # for any account.
        cookies=http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(allowed_domains=[])),
        headers={"User-Agent": "NanoSaaS-Webhooks"},
    )


async def send(
    client: httpx.AsyncClient, url: str, secret: str, message_id: str, payload: str
) -> tuple[int | None, str, str | None]:
    """
    Send an event to a webhook's URL, signed, within DEADLINE seconds: the
    status of the answer, if there was one, how it went, as the page shows
    it, and its Retry-After header. It tries each address of the host until
    one takes the connection.
    """
    try:
        async with asyncio.timeout(DEADLINE):
            parsed, host, port = parse(url)
            addresses = await resolve(host, port)
            body = payload.encode()
            timestamp = int(time.time())
            headers = {
                # The URL goes to an address checked above, and the host's name goes in
                # the Host header and in TLS, which checks the certificate against it.
                "Host": parsed.netloc.decode(),
                "Content-Type": "application/json",
                "webhook-id": message_id,
                "webhook-timestamp": str(timestamp),
                "webhook-signature": sign(secret, message_id, timestamp, body),
            }
            for address in addresses:
                try:
                    async with client.stream(
                        "POST", parsed.copy_with(host=address), content=body, headers=headers,
                        extensions={"sni_hostname": host},
                    ) as response:
                        # Without reading the body: only the status matters, and how long to wait.
                        answer = f"{response.status_code} {response.reason_phrase}"
                        return response.status_code, answer, response.headers.get("retry-after")
                except httpx.ConnectError as e:
                    error = e
            raise error
    except Undeliverable as e:
        return None, str(e), None
    except (TimeoutError, httpx.TimeoutException):
        return None, "Timeout", None
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}", None


async def claim_due(limit: int = BATCH) -> list[WebhookDelivery]:
    """
    Claim the pending deliveries that are due, up to limit, for CLAIM, but
    not those of disabled webhooks, nor of the ones whose events wait (see
    throttle()). Every worker process looks for them: the conditional update
    of next_attempt_at lets only one of them take each delivery, like
    TaskHistory.start() with the tasks.
    """
    if limit <= 0:
        return []
    now = timezone.now()
    due = await WebhookDelivery.filter(
        Q(webhook__throttled_until__isnull=True) | Q(webhook__throttled_until__lte=now),
        status="PENDING", next_attempt_at__lte=now, webhook__disabled_at__isnull=True,
    ).order_by("next_attempt_at").limit(limit).prefetch_related("webhook")
    claimed = []
    for delivery in due:
        until = now + CLAIM
        if await WebhookDelivery.filter(
            id=delivery.id, status="PENDING", next_attempt_at=delivery.next_attempt_at
        ).update(next_attempt_at=until, attempts=F("attempts") + 1):
            delivery.next_attempt_at = until
            delivery.attempts += 1
            claimed.append(delivery)
    return claimed


async def attempt(client: httpx.AsyncClient, delivery: WebhookDelivery) -> None:
    """An attempt of a delivery this worker claimed: send it, and save how it went."""
    webhook = delivery.webhook
    try:
        status, answer, asked = await send(client, webhook.url, webhook.secret, delivery.message_id, delivery.payload)
    except Exception:
        # Like a malformed secret: an attempt that failed all the same, so that the delivery
        # ends. The log has the traceback, the page only this.
        logger.exception("Can't send webhook delivery %s", delivery.message_id)
        status, answer, asked = None, "Error: the event couldn't be sent", None

    now = timezone.now()
    # How long the receiver asked to wait, if it did: up to MAX_WAIT.
    wait = retry_after(asked, now)
    if wait is not None:
        wait = min(wait, MAX_WAIT.total_seconds())
    delivered = status is not None and 200 <= status < 300
    fields = {"last_attempt_at": now, "last_response": answer[:200]}
    where = (delivery.message_id, host_of(webhook.url))
    if delivered:
        fields["status"] = "DELIVERED"
    elif status == 410 or delivery.attempts >= len(RETRIES):
        fields["status"] = "FAILED"
        logger.warning(
            "Webhook delivery %s to %s failed for good, at attempt %d of %d: %s",
            *where, delivery.attempts, len(RETRIES), answer,
        )
    else:
        delay = RETRIES[delivery.attempts] * random.uniform(1, 1.1)
        # Not sooner than the receiver asked.
        if wait is not None:
            delay = max(delay, wait)
        fields["next_attempt_at"] = now + datetime.timedelta(seconds=delay)
        logger.info("Webhook delivery %s to %s failed, attempt %d of %d: %s", *where, delivery.attempts, len(RETRIES), answer)
    try:
        # Only while this worker's claim holds.
        await WebhookDelivery.filter(id=delivery.id, next_attempt_at=delivery.next_attempt_at).update(**fields)
        await track(webhook, status, answer, now)
        # The webhook's other events wait too, as long as it asked, or THROTTLE if it's under load.
        if not delivered and (wait is not None or status in UNDER_LOAD):
            await throttle(webhook, now + (THROTTLE if wait is None else datetime.timedelta(seconds=wait)))
    except Exception as e:
        # Its claim ends, and then a worker sends it again.
        logger.error("Can't save the attempt of webhook delivery %s: %s", delivery.message_id, e)


def retry_after(header: str | None, now: datetime.datetime) -> float | None:
    """
    The seconds that an answer's Retry-After header asks to wait: a whole
    number of them, or a date, which may have passed. None without one, or
    with one that's neither.
    """
    if header is None:
        return None
    header = header.strip()
    if header.isascii() and header.isdigit():
        return float(header)
    try:
        moment = email.utils.parsedate_to_datetime(header)
    except Exception:
        # Whatever the receiver wrote: an attempt must be saved even then, or its
        # claim would end, again and again. A huge year overflows in Python 3.11.
        return None
    if moment.tzinfo is None:
        # A date with -0000, which the standard reads as UTC too.
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (moment - now).total_seconds())


async def throttle(webhook: Webhook, until: datetime.datetime) -> None:
    """
    Make a webhook's events wait until then, unless they wait longer already:
    the workers don't claim them meanwhile, and their attempts aren't spent.
    Only while its URL is the one the attempt went to, like track().
    """
    if await Webhook.filter(
        Q(throttled_until__isnull=True) | Q(throttled_until__lt=until), id=webhook.id, url=webhook.url
    ).update(throttled_until=until):
        logger.info(
            "The events of the webhook of account %d, to %s, wait until %s",
            webhook.account_id, host_of(webhook.url), until.isoformat(timespec="seconds"),
        )


async def track(webhook: Webhook, status: int | None, answer: str, now: datetime.datetime) -> None:
    """
    Keep how a webhook's attempts go, after one of them: since when they
    fail, if they do, and disable the webhook after a 410, or once they
    failed for DISABLE_AFTER. Only while its URL is the one the attempt went
    to: a new one starts afresh.
    """
    same_url = Webhook.filter(id=webhook.id, url=webhook.url)
    if status is not None and 200 <= status < 300:
        await same_url.filter(failing_since__isnull=False).update(failing_since=None)
        return
    await same_url.filter(failing_since__isnull=True).update(failing_since=now)
    if status == 410:
        reason, due = f"Its URL answered {answer}: it wants no more events.", same_url
    else:
        reason = f"Its attempts failed for {DISABLE_AFTER.days} days in a row. The last one: {answer}."
        due = same_url.filter(failing_since__lte=now - DISABLE_AFTER)
    if await due.filter(disabled_at__isnull=True).update(disabled_at=now, disabled_reason=reason[:300]):
        logger.warning(
            "The webhook of account %d, to %s, was disabled: %s", webhook.account_id, host_of(webhook.url), reason
        )


async def cleanup() -> None:
    """
    Delete the deliveries that ended, delivered or failed, more than KEEP
    ago, and the ones that waited longer than that for their webhook,
    disabled.
    """
    now = timezone.now()
    await WebhookDelivery.filter(status__in=["DELIVERED", "FAILED"], last_attempt_at__lt=now - KEEP).delete()
    await WebhookDelivery.filter(
        status="PENDING", created_at__lt=now - KEEP,
        webhook_id__in=Subquery(Webhook.filter(disabled_at__isnull=False).values("id")),
    ).delete()


async def deliver_webhooks() -> None:
    """
    Every POLL seconds, send the deliveries that are due, and every hour,
    delete the old ones. Every worker process runs it, from its startup to
    its shutdown.
    """
    sending: set[asyncio.Task] = set()
    cleaned = -math.inf
    failing = False
    async with client() as http_client:
        try:
            while True:
                try:
                    for delivery in await claim_due(BATCH - len(sending)):
                        task = asyncio.create_task(attempt(http_client, delivery))
                        sending.add(task)
                        task.add_done_callback(sending.discard)
                    if time.monotonic() - cleaned > 3600:
                        await cleanup()
                        cleaned = time.monotonic()
                    if failing:
                        logger.info("Webhooks: the database answers again.")
                    failing = False
                except Exception as e:
                    # Once, not every second.
                    if not failing:
                        logger.error("Can't look for the webhook deliveries that are due: %s", e)
                    failing = True
                await asyncio.sleep(POLL)
        finally:
            # Their claims end, and then a worker sends them again.
            for task in sending:
                task.cancel()
            await asyncio.gather(*sending, return_exceptions=True)
