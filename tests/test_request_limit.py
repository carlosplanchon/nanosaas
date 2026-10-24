"""
The limit of each account's requests to the API: MAX_REQUESTS_PER_MINUTE a
minute, with all its keys, counted in Redis. The next ones get a 429 error
until the next minute. The panel's requests don't count, and a Redis that
doesn't answer doesn't hold up the API.
"""
import asyncio
import secrets
import time

import pytest
from redis.asyncio import Redis

from tests.helpers import Proxy, client_for, create_account, stored_api_key

pytestmark = pytest.mark.anyio


async def with_time_left_in_the_minute(seconds=10):
    """Wait for the next minute if this one has less than `seconds` left: the requests that follow count in a single one."""
    left = 60 - time.time() % 60
    if left < seconds:
        await asyncio.sleep(left + 0.1)


async def statuses(client, path, keys):
    """The statuses of a GET to the path with each key, one after the other."""
    return [(await client.get(path, headers={"X-API-Key": key})).status_code for key in keys]


async def test_an_account_gets_a_429_over_its_requests_of_a_minute_until_the_next_one(request_limit_server, account):
    headers = {"X-API-Key": account.api_key}
    async with client_for(request_limit_server) as anon:
        await with_time_left_in_the_minute()
        # Whatever they answer: a 404 counts too.
        assert await statuses(anon, "/api/user_credits", [account.api_key] * 4) == [200] * 4
        assert (await anon.get("/api/task_details/no-such-task", headers=headers)).status_code == 404

        before = time.time()
        r = await anon.get("/api/credit_history", headers=headers)
        after = time.time()
        assert r.status_code == 429
        retry_after = int(r.headers["retry-after"])
        assert r.json() == {
            "detail": f"Too many requests: an account can make up to 5 a minute. Try again in {retry_after} seconds."
        }
        # The seconds until the next minute.
        next_minute = (before // 60 + 1) * 60
        assert next_minute - after <= retry_after <= next_minute - before + 1

        # Before the route does anything: no task starts, and no credit goes.
        r = await anon.post("/api/task/divide", json={"x": 1, "y": 1}, headers=headers)
        assert r.status_code == 429
        assert await account.credits() == 5

        # In the next minute, they go on.
        await asyncio.sleep(retry_after)
        r = await anon.get("/api/user_credits", headers=headers)
        assert (r.status_code, r.json()) == (200, {"credits": 5})


async def test_all_the_keys_of_an_account_count_together(request_limit_server, db, account):
    other_key = secrets.token_hex(32)
    await db.execute(
        "INSERT INTO apikey (account_id, name, key_hash, created_at) VALUES ($1, 'Other', $2, now())",
        account.id, stored_api_key(other_key),
    )
    async with client_for(request_limit_server) as anon:
        await with_time_left_in_the_minute()
        assert await statuses(anon, "/api/user_credits", [account.api_key, other_key] * 3) == [200] * 5 + [429]


async def test_each_account_has_its_own_and_the_panel_s_requests_don_t_count(request_limit_server, db, account):
    other = await create_account(db)
    async with client_for(request_limit_server) as anon, client_for(request_limit_server, account.cookie) as user:
        await with_time_left_in_the_minute()
        for _ in range(10):
            assert (await user.get("/user/credits")).status_code == 200
        assert await statuses(anon, "/api/user_credits", [account.api_key] * 6) == [200] * 5 + [429]
        # Its panel goes on, and so do the other accounts' requests.
        assert (await user.get("/user/credits")).status_code == 200
        assert await statuses(anon, "/api/user_credits", [other.api_key]) == [200]


async def test_redis_keeps_each_count_two_minutes(request_limit_server, services, account):
    redis = Redis.from_url(services.redis_url.rsplit("/", 1)[0] + "/2")
    try:
        async with client_for(request_limit_server) as anon:
            await with_time_left_in_the_minute()
            assert await statuses(anon, "/api/user_credits", [account.api_key]) == [200]
            # The minute's, which nothing counts in afterwards: they don't pile up.
            key = f"nanosaas-requests:{account.id}:{int(time.time() // 60)}"
            assert int(await redis.get(key)) == 1
            assert 110 < await redis.ttl(key) <= 120
    finally:
        await redis.aclose()


@pytest.mark.parametrize("fault", ["hang", "close"])
async def test_a_redis_that_doesnt_answer_doesnt_hold_up_the_api(services, start_server, account, fault):
    host, port = services.redis_url.removeprefix("redis://").split("/")[0].rsplit(":", 1)
    proxy = Proxy(host, int(port))
    try:
        server = start_server(
            f"requests-{fault}", broker=f"redis://{proxy.address[0]}:{proxy.address[1]}/2", max_requests_per_minute=3
        )
        async with client_for(server) as anon:
            await with_time_left_in_the_minute()
            assert await statuses(anon, "/api/credit_history", [account.api_key] * 4) == [200] * 3 + [429]
            getattr(proxy, fault)()
            # The requests go on without their limit, without waiting for Redis more than half a second.
            for _ in range(3):
                started = time.monotonic()
                assert await statuses(anon, "/api/credit_history", [account.api_key]) == [200]
                assert time.monotonic() - started < 2
        # Once a minute at most, instead of on each request.
        log = (server.config.parent / "uvicorn.log").read_text()
        assert log.count("Redis doesn't count the API's requests") == 1
    finally:
        # Before the server stops, so it doesn't wait for the hung connections.
        proxy.stop()


@pytest.mark.parametrize("now, minute, retry_after", [
    (120.0, 2, 60), (120.4, 2, 60), (150.0, 2, 30), (179.0, 2, 1), (179.9, 2, 1), (180.0, 3, 60),
])
def test_a_request_counts_in_its_minute_until_the_next_one(app_module, now, minute, retry_after):
    assert app_module.request_window(now) == (minute, retry_after)
