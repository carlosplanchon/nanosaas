"""
Fixtures: PostgreSQL and Redis in Docker (with testcontainers), and the API
and a TaskIQ worker running as separate processes, like in production.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests.helpers import ADMIN_EMAIL, SECRET, client_for, create_account

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class Services:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    redis_url: str

    @property
    def dsn(self):
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@pytest.fixture(scope="session")
def services():
    postgres = PostgresContainer("postgres:17-alpine", driver=None)
    redis = DockerContainer("redis:7-alpine").with_exposed_ports(6379).waiting_for(
        LogMessageWaitStrategy("Ready to accept connections")
    )
    with postgres, redis:
        host = redis.get_container_host_ip()
        yield Services(
            db_host=postgres.get_container_host_ip(),
            db_port=int(postgres.get_exposed_port(5432)),
            db_name=postgres.dbname,
            db_user=postgres.username,
            db_password=postgres.password,
            redis_url=f"redis://{host}:{redis.get_exposed_port(6379)}/0",
        )


def write_config(
    path, services, port, root_path="", broker=None, db_name=None, initial_credits=None, admins=None,
    max_pending_tasks=None, max_body_size=None, allow_private_webhooks=None, db_address=None, **taskiq
):
    """
    db_address: another (host, port) for PostgreSQL, like a proxy's.
    taskiq: more settings for the [taskiq] section, like time_limit=3.
    """
    db_host, db_port = db_address or (services.db_host, services.db_port)
    broker = broker or services.redis_url
    app = "" if initial_credits is None else f"INITIAL_CREDITS = {initial_credits}\n"
    app += "" if admins is None else f"ADMINS = {json.dumps(admins)}\n"
    app += "" if max_pending_tasks is None else f"MAX_PENDING_TASKS = {max_pending_tasks}\n"
    app += "" if max_body_size is None else f"MAX_BODY_SIZE = {max_body_size}\n"
    app += "" if allow_private_webhooks is None else f"ALLOW_PRIVATE_WEBHOOKS = {json.dumps(allow_private_webhooks)}\n"
    settings = "".join(f"{key} = {value}\n" for key, value in taskiq.items())
    path.write_text(f"""\
[app]
ROOT_PATH = "{root_path}"
{app}
[api]
JWT_SIGNING_SECRET_KEY = "{SECRET}"
GOOGLE_CLIENT_ID = "test-client-id"
GOOGLE_CLIENT_SECRET = "test-client-secret"
GOOGLE_REDIRECT_URI = "http://127.0.0.1:{port}/auth/callback"

[database]
DB_HOST = "{db_host}"
DB_PORT = {db_port}
DB_NAME = "{db_name or services.db_name}"
DB_USERNAME = "{services.db_user}"
DB_PASSWORD = "{services.db_password}"

[taskiq]
broker = "{broker}"
backend = "{broker}"
{settings}""")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Process:
    """A process started from the repository root, reading the given config."""

    def __init__(self, args, config, log, is_ready, timeout=60, env=None):
        self.log = log
        # Unbuffered, so the logs are complete when a test reads them.
        env = {**os.environ, "NANOSAAS_CONFIG": str(config), "PYTHONUNBUFFERED": "1", **(env or {})}
        with log.open("w") as out:
            # Its own session, so stop() also reaches the worker's pool.
            self.proc = subprocess.Popen(
                args, cwd=REPO, env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        deadline = time.monotonic() + timeout
        while not is_ready():
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError(f"{args[2:4]} didn't start:\n{log.read_text()[-3000:]}")
            time.sleep(0.2)

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=20)
            # The worker's processes may outlive the main one: wait for them too.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                os.killpg(self.proc.pid, 0)
                time.sleep(0.1)
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()

    def kill(self):
        """Like a power cut: the process can't do anything before it dies."""
        os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()


@dataclass
class Server:
    url: str
    config: Path


def run_server(tmp_path_factory, services, name, **config):
    directory = tmp_path_factory.mktemp(name)
    port = free_port()
    server = Server(f"http://127.0.0.1:{port}", directory / "config.toml")
    write_config(server.config, services, port, **config)

    def is_ready():
        try:
            return httpx.get(f"{server.url}/openapi.json").status_code == 200
        except httpx.TransportError:
            return False

    args = [sys.executable, "-m", "uvicorn", "main_api:app", "--host", "127.0.0.1", "--port", str(port)]
    return Process(args, server.config, directory / "uvicorn.log", is_ready), server


@pytest.fixture(scope="session")
def server(tmp_path_factory, services):
    """The API, with the tests' admin. It applies the migrations when it starts."""
    process, server = run_server(tmp_path_factory, services, "server", admins=[ADMIN_EMAIL])
    yield server
    process.stop()


@pytest.fixture
def start_server(tmp_path_factory, services):
    """Starts APIs with other settings, like db_name, and stops them after the test."""
    processes = []

    def start(name, **config):
        process, server = run_server(tmp_path_factory, services, name, **config)
        processes.append(process)
        return server

    yield start
    for process in processes:
        process.stop()


def run_worker(server, *options):
    """
    A TaskIQ worker with the server's config, plus the test-only tasks, and a
    temporary directory of its own, `heartbeats`, where its process writes them.
    options: more of its command-line options, like "--wait-tasks-timeout", "1".
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    log = server.config.parent / f"worker-{name}.log"
    heartbeats = server.config.parent / f"tmp-{name}"
    heartbeats.mkdir()
    args = [
        sys.executable, "-m", "taskiq", "worker", "tests.worker_app:broker",
        "--receiver", "taskiq_task:Receiver", "--workers", "1", *options,
    ]
    process = Process(
        args, server.config, log, lambda: "Listening started." in log.read_text(), env={"TMPDIR": str(heartbeats)}
    )
    process.heartbeats = heartbeats
    return process


@pytest.fixture(scope="session")
def worker(server):
    """The API's worker."""
    process = run_worker(server)
    yield process
    process.stop()


@pytest.fixture
def start_worker():
    """Starts workers for a server, which the test can kill, and stops them after it."""
    processes = []

    def start(server, *options):
        processes.append(run_worker(server, *options))
        return processes[-1]

    yield start
    for process in processes:
        process.stop()


@pytest.fixture(scope="session")
def prefixed_server(tmp_path_factory, services, server):
    """The API with ROOT_PATH = "nanosaas"."""
    process, prefixed = run_server(tmp_path_factory, services, "prefixed", root_path="nanosaas")
    yield prefixed
    process.stop()


@pytest.fixture(scope="session")
def broker_down_server(tmp_path_factory, services, server):
    """The API with its broker pointing to a port where nothing listens."""
    # Bound but not listening: connections are refused and the API can't get this port.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        broker = f"redis://127.0.0.1:{reserved.getsockname()[1]}/0"
        process, broken = run_server(tmp_path_factory, services, "broker-down", broker=broker)
        yield broken
        process.stop()


@pytest.fixture(scope="session")
def short_limits_server(tmp_path_factory, services, server):
    """
    The API with queue_timeout = 2 and time_limit = 4, on its own Redis
    database: the tests can kill its workers without touching the others.
    """
    broker = services.redis_url.rsplit("/", 1)[0] + "/1"
    process, short = run_server(
        tmp_path_factory, services, "short-limits", broker=broker, queue_timeout=2, time_limit=4
    )
    yield short
    process.stop()


@pytest.fixture(scope="session")
def retry_server(tmp_path_factory, services, server):
    """
    The API with lease = 2 and attempts = 2: the tests can kill its workers,
    and the others queue their tasks again soon. On its own Redis and
    PostgreSQL databases, so the other APIs' workers don't see its tasks,
    whose leases expire.
    """
    asyncio.run(create_database(services, "retry"))
    broker = services.redis_url.rsplit("/", 1)[0] + "/4"
    process, retry = run_server(
        tmp_path_factory, services, "retry", broker=broker, db_name="retry", lease=2, attempts=2
    )
    yield retry
    process.stop()


@pytest.fixture(scope="session")
def limited_server(tmp_path_factory, services, server):
    """
    The API with MAX_PENDING_TASKS = 3, on its own Redis database, which no
    worker reads: its tasks stay pending.
    """
    broker = services.redis_url.rsplit("/", 1)[0] + "/8"
    process, limited = run_server(tmp_path_factory, services, "limited", broker=broker, max_pending_tasks=3)
    yield limited
    process.stop()


async def create_database(services, name):
    conn = await asyncpg.connect(services.dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture
async def retry_db(services, retry_server):
    """A connection to the database of retry_server."""
    conn = await asyncpg.connect(services.dsn.rsplit("/", 1)[0] + "/retry")
    yield conn
    await conn.close()


@pytest.fixture
async def retry_account(retry_db):
    """An account in the database of retry_server."""
    return await create_account(retry_db)


@pytest.fixture(scope="session")
def webhook_server(tmp_path_factory, services, server):
    """
    The API with ALLOW_PRIVATE_WEBHOOKS, so webhooks can go to the tests'
    receivers, on 127.0.0.1. On its own Redis and PostgreSQL databases, so
    only its worker, webhook_worker, sends its webhooks' deliveries.
    """
    asyncio.run(create_database(services, "webhooks"))
    broker = services.redis_url.rsplit("/", 1)[0] + "/9"
    process, webhooks = run_server(
        tmp_path_factory, services, "webhooks", broker=broker, db_name="webhooks", allow_private_webhooks=True
    )
    yield webhooks
    process.stop()


@pytest.fixture(scope="session")
def webhook_worker(webhook_server):
    """The worker of webhook_server."""
    process = run_worker(webhook_server)
    yield process
    process.stop()


@pytest.fixture
async def webhook_db(services, webhook_server):
    """A connection to the database of webhook_server."""
    conn = await asyncpg.connect(services.dsn.rsplit("/", 1)[0] + "/webhooks")
    yield conn
    await conn.close()


@pytest.fixture
async def webhook_account(webhook_db):
    """An account in the database of webhook_server."""
    return await create_account(webhook_db)


@pytest.fixture(scope="session")
def app_module(tmp_path_factory, services, server):
    """main_api imported in this process, for the tests that stub parts of it."""
    config = tmp_path_factory.mktemp("in-process") / "config.toml"
    # New accounts start without credits, as on a public server (see test_auth.py).
    write_config(config, services, port=8000, initial_credits=0)
    os.environ["NANOSAAS_CONFIG"] = str(config)
    # The templates and the static files are relative to the current directory.
    os.chdir(REPO)
    import main_api
    return main_api


@pytest.fixture
async def taskiq_client(app_module):
    """The API's TaskIQ broker and result backend, which the worker's use too."""
    await app_module.broker.startup()
    yield app_module.broker
    # Its Redis connections belong to this test's event loop.
    await app_module.broker.shutdown()


@pytest.fixture
async def db(services, server):
    conn = await asyncpg.connect(services.dsn)
    yield conn
    await conn.close()


@pytest.fixture
async def account(db):
    """A new account with 5 credits, so the tests don't share balances."""
    return await create_account(db)


@pytest.fixture
async def user(server, account):
    """HTTP client logged in as `account`."""
    async with client_for(server, account.cookie) as client:
        yield client


@pytest.fixture
async def anon(server):
    """HTTP client without a session (for the API key routes)."""
    async with client_for(server) as client:
        yield client
