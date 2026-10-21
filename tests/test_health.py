"""
The health check, /health: 200 while the app can use PostgreSQL and Redis,
and 503 when one of them doesn't answer, down or hung, without waiting
for it more than HEALTH_TIMEOUT seconds.
"""
import asyncio
import contextlib
import threading
import time
from pathlib import Path

import pytest

from tests.helpers import client_for

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
OK = {"status": "ok", "postgres": "ok", "redis": "ok"}


class Proxy:
    """
    A TCP proxy to a server, on a thread of its own, which the test can make
    stop answering, like a server that hangs, or close, like one that's down.
    """

    def __init__(self, host, port):
        self.server_address = (host, port)
        self.hung = False
        self.writers = set()
        self.relays = set()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.server = self.run(asyncio.start_server(self.relay, "127.0.0.1", 0))
        self.address = ("127.0.0.1", self.server.sockets[0].getsockname()[1])

    def run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    async def relay(self, client_reader, client_writer):
        self.relays.add(asyncio.current_task())
        server_reader, server_writer = await asyncio.open_connection(*self.server_address)
        self.writers |= {client_writer, server_writer}

        async def pipe(reader, writer):
            with contextlib.suppress(ConnectionError):
                while data := await reader.read(65536):
                    while self.hung:
                        await asyncio.sleep(0.1)
                    writer.write(data)
                    await writer.drain()
            writer.close()

        await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer))

    def hang(self):
        """What goes through it doesn't arrive: the server seems to hang."""
        self.hung = True

    def close(self):
        """Its connections end, and new ones are refused: the server seems down."""
        async def close():
            self.server.close()
            for writer in self.writers:
                writer.close()

        # The relays it held end too.
        self.hung = False
        self.run(close())

    def stop(self):
        self.close()

        async def relays_ended():
            pending = {relay for relay in self.relays if not relay.done()}
            if pending:
                await asyncio.wait(pending, timeout=5)

        self.run(relays_ended())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


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
