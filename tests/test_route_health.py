import os

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from northgate.route_health import RouteHealthEngine


@pytest.mark.anyio
async def test_route_circuit_opens_and_recovers_through_single_probe() -> None:
    redis = Redis.from_url(os.environ.get("NORTHGATE_TEST_REDIS_URL", "redis://127.0.0.1:6379/15"))
    try:
        await redis.ping()
    except RedisError:
        await redis.aclose()
        pytest.skip("Redis is not available")

    await redis.flushdb()
    engine = RouteHealthEngine(redis)
    try:
        assert (
            await engine.allow(
                route_key="primary",
                token="request-1",
                now_ms=1_000,
                recovery_seconds=10,
            )
        ).allowed
        await engine.record_failure(
            route_key="primary",
            token="request-1",
            now_ms=1_000,
            threshold=2,
            recovery_seconds=10,
        )
        assert (
            await engine.allow(
                route_key="primary",
                token="request-2",
                now_ms=2_000,
                recovery_seconds=10,
            )
        ).allowed
        await engine.record_failure(
            route_key="primary",
            token="request-2",
            now_ms=2_000,
            threshold=2,
            recovery_seconds=10,
        )

        denied = await engine.allow(
            route_key="primary",
            token="request-3",
            now_ms=3_000,
            recovery_seconds=10,
        )
        assert denied.allowed is False

        probe = await engine.allow(
            route_key="primary",
            token="probe-1",
            now_ms=12_001,
            recovery_seconds=10,
        )
        concurrent = await engine.allow(
            route_key="primary",
            token="probe-2",
            now_ms=12_001,
            recovery_seconds=10,
        )
        assert probe.allowed and probe.probe
        assert concurrent.allowed is False

        await engine.record_success(route_key="primary", token="probe-1", now_ms=12_002)
        recovered = await engine.allow(
            route_key="primary",
            token="request-4",
            now_ms=12_003,
            recovery_seconds=10,
        )
        assert recovered.allowed and recovered.probe is False
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
