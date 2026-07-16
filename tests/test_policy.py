import asyncio
import os
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from northgate.policy import PolicyEngine, PolicyRejectedError
from northgate.routing import PolicyLimits


@pytest.mark.anyio
async def test_atomic_admission_and_idempotent_token_settlement() -> None:
    redis = Redis.from_url(os.environ.get("NORTHGATE_TEST_REDIS_URL", "redis://127.0.0.1:6379/15"))
    try:
        await redis.ping()
    except RedisError:
        await redis.aclose()
        pytest.skip("Redis is not available")

    await redis.flushdb()
    engine = PolicyEngine(redis, lease_seconds=30)
    try:
        request_limits = PolicyLimits(requests_per_minute=2)
        results = await asyncio.gather(
            *(
                engine.admit(
                    gateway_key="rate-test",
                    request_id=f"req_rate_{index}",
                    limits=request_limits,
                    estimated_tokens=0,
                )
                for index in range(3)
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, BaseException) for result in results) == 2
        rejection = next(result for result in results if isinstance(result, PolicyRejectedError))
        assert rejection.code == "REQUEST_LIMIT_EXCEEDED"

        token_limits = PolicyLimits(concurrent_requests=1, tokens_per_day=35)
        first = await engine.admit(
            gateway_key="token-test",
            request_id="req_token_1",
            limits=token_limits,
            estimated_tokens=30,
        )
        with pytest.raises(PolicyRejectedError, match="Concurrency limit exceeded"):
            await engine.admit(
                gateway_key="token-test",
                request_id="req_token_concurrent",
                limits=token_limits,
                estimated_tokens=1,
            )

        await engine.settle(first, 10)
        await engine.settle(first, 10)
        with pytest.raises(PolicyRejectedError, match="Token limit exceeded"):
            await engine.admit(
                gateway_key="token-test",
                request_id="req_token_too_large",
                limits=token_limits,
                estimated_tokens=30,
            )

        second = await engine.admit(
            gateway_key="token-test",
            request_id="req_token_2",
            limits=token_limits,
            estimated_tokens=20,
        )
        await engine.settle(second, 10)
        day = datetime.now(UTC).strftime("%Y%m%d")
        used = await redis.hget(f"northgate:policy:{{token-test}}:tokens:{day}", "used")
        assert used == b"20"
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
