"""
The health check, /health: 200 while the app can use PostgreSQL and Redis,
and 503 when one of them doesn't answer, down or hung, without waiting
for it more than HEALTH_TIMEOUT seconds.
"""
import time
from pathlib import Path

import pytest

from tests.helpers import Proxy, client_for

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
OK = {"status": "ok", "postgres": "ok", "redis": "ok"}


async def timed_health(client):
    started = time.monotonic()
    r = await client.get("/health")
    return r, time.monotonic() - started


async def test_the_app_is_healthy_with_postgresql_and_redis(anon):
    r = await anon.get("/health")
    assert (r.status_code, r.json()) == (200, OK)


async def test_without_redis_it_isnt(broker_down_server):
    async with client_for(broker_down_server) as client:
        r, seconds = await timed_health(client)
    assert (r.status_code, r.json()) == (503, {"status": "error", "postgres": "ok", "redis": "error"})
    assert seconds < 4


@pytest.mark.parametrize("fault", ["hang", "close"])
async def test_without_postgresql_it_isnt(services, start_server, fault):
    proxy = Proxy(services.db_host, services.db_port)
    try:
        server = start_server(f"health-{fault}", db_address=proxy.address)
        async with client_for(server) as client:
            assert (await client.get("/health")).json() == OK
            getattr(proxy, fault)()
            r, seconds = await timed_health(client)
        assert (r.status_code, r.json()) == (503, {"status": "error", "postgres": "error", "redis": "ok"})
        # A hung database doesn't hang the check: it gives up after HEALTH_TIMEOUT, 2 seconds.
        assert seconds < 4
        assert "Health check: PostgreSQL doesn't answer" in (server.config.parent / "uvicorn.log").read_text()
    finally:
        # Before the server stops, so it doesn't wait for the hung connections.
        proxy.stop()


def test_compose_checks_the_health_of_the_api():
    compose = (REPO / "compose.yaml").read_text()
    assert "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" in compose
