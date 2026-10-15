"""Migrations: the API applies them when it starts, also on databases from before them."""
import uuid
from pathlib import Path

import asyncpg
import pytest
from tortoise import Tortoise
from tortoise.context import TortoiseContext
from tortoise.migrations.autodetector import MigrationAutodetector

from tests.helpers import client_for, create_account, insert_task

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent


async def test_a_new_database_gets_every_migration(db):
    files = sorted(path.stem for path in (REPO / "web" / "migrations").glob("0*.py"))
    rows = await db.fetch("SELECT app, name FROM tortoise_migrations ORDER BY name")
    assert [tuple(row) for row in rows] == [("models", name) for name in files]


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

    # Older versions created the tables with generate_schemas.
    host = f"{services.db_user}:{services.db_password}@{services.db_host}:{services.db_port}"
    await Tortoise.init(db_url=f"postgres://{host}/{name}", modules={"models": ["web.db_models"]})
    try:
        await Tortoise.generate_schemas()
    finally:
        await Tortoise.close_connections()

    db = await asyncpg.connect(f"postgresql://{host}/{name}")
    try:
        account = await create_account(db)
        task_id = await insert_task(db, account, status="SUCCESS", result=2.0)

        # Twice: the second start finds the first migration already recorded.
        for attempt in ("first", "second"):
            server = start_server(f"legacy-{attempt}", db_name=name)
            async with client_for(server) as anon:
                headers = {"X-API-Key": account.api_key}
                r = await anon.get(f"/api/task_details/{task_id}", headers=headers)
                assert (r.json()["status"], r.json()["result"]) == ("SUCCESS", 2.0)
                r = await anon.get("/api/user_credits", headers=headers)
                assert r.json() == {"credits": 5}
            rows = await db.fetch("SELECT app, name FROM tortoise_migrations")
            assert [tuple(row) for row in rows] == [("models", "0001_initial")]
    finally:
        await db.close()
