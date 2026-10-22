"""Migrations: the API applies them when it starts, also on databases from before them."""
import uuid
from pathlib import Path

import asyncpg
import pytest
from tortoise import Tortoise
from tortoise.context import TortoiseContext
from tortoise.migrations.api import migrate
from tortoise.migrations.autodetector import MigrationAutodetector

from tests.helpers import client_for, create_account

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = [("models", path.stem) for path in sorted((REPO / "web" / "migrations").glob("0*.py"))]


async def test_a_new_database_gets_every_migration(db):
    rows = await db.fetch("SELECT app, name FROM tortoise_migrations ORDER BY name")
    assert [tuple(row) for row in rows] == MIGRATIONS


async def test_the_pending_tasks_are_found_by_an_index(db):
    # The workers look for the ones whose lease expired every few seconds.
    indexdef = await db.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'taskhistory' AND indexdef LIKE '%(status)'"
    )
    assert indexdef is not None


async def test_the_pages_of_the_history_are_found_by_an_index(db):
    # Each one starts where the index has the account's last task of the one before.
    indexdef = await db.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'taskhistory' "
        "AND indexdef LIKE '%(user_id, created_at, id)'"
    )
    assert indexdef is not None


async def test_the_migrations_match_the_models(app_module):
    async with TortoiseContext() as ctx:
        await ctx.init(config=app_module.TORTOISE_ORM)
        changes = await MigrationAutodetector(ctx.apps, app_module.TORTOISE_ORM["apps"]).changes()
    assert not changes, "The models changed: run tortoise -c shared.TORTOISE_ORM makemigrations"


async def test_a_database_from_before_the_migrations_keeps_its_data(services, start_server):
    name = f"legacy_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(services.dsn)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()

    # Older versions created the tables with generate_schemas, as they are in the
    # first migration, without recording any migration.
    host = f"{services.db_user}:{services.db_password}@{services.db_host}:{services.db_port}"
    config = {
        "connections": {"default": f"postgres://{host}/{name}"},
        "apps": {
            "models": {"models": ["web.db_models"], "default_connection": "default", "migrations": "web.migrations"}
        },
    }
    try:
        await migrate(config=config, target="models.0001_initial", direction="forward")
    finally:
        await Tortoise.close_connections()

    db = await asyncpg.connect(f"postgresql://{host}/{name}")
    try:
        await db.execute("DROP TABLE tortoise_migrations")
        account = await create_account(db)
        # With the columns of the first migration's table.
        task_id = str(uuid.uuid4())
        await db.execute(
            """
            INSERT INTO taskhistory (task_id, task_type, parameters, status, result, created_at, user_id)
            VALUES ($1, 'divide', '{"x": 6, "y": 3}', 'SUCCESS', '2.0', now(), $2)
            """,
            task_id, account.id,
        )

        # Twice: the second start finds the migrations already recorded.
        for attempt in ("first", "second"):
            server = start_server(f"legacy-{attempt}", db_name=name)
            async with client_for(server) as anon:
                headers = {"X-API-Key": account.api_key}
                r = await anon.get(f"/api/task_details/{task_id}", headers=headers)
                # It cost a credit, the default of its column.
                assert (r.json()["status"], r.json()["result"], r.json()["cost"]) == ("SUCCESS", 2.0, 1)
                r = await anon.get("/api/user_credits", headers=headers)
                assert r.json() == {"credits": 5}
                # Its credit history starts with its balance, once.
                r = await anon.get("/api/credit_history", headers=headers)
                history = [(m["reason"], m["amount"], m["balance"]) for m in r.json()["movements"]]
                assert history == [("opening", 5, 5)]
            rows = await db.fetch("SELECT app, name FROM tortoise_migrations ORDER BY name")
            assert [tuple(row) for row in rows] == MIGRATIONS
    finally:
        await db.close()
