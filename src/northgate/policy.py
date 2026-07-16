import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from northgate.routing import PolicyLimits

logger = structlog.get_logger()

_ADMIT_SCRIPT = """
local request_limit = tonumber(ARGV[1])
local concurrency_limit = tonumber(ARGV[2])
local token_limit = tonumber(ARGV[3])
local estimated_tokens = tonumber(ARGV[4])
local request_id = ARGV[5]
local now_ms = tonumber(ARGV[6])
local lease_ms = tonumber(ARGV[7])
local request_ttl_ms = tonumber(ARGV[8])
local token_ttl_ms = tonumber(ARGV[9])

local request_current = tonumber(redis.call('GET', KEYS[1]) or '0')
if request_limit > 0 and request_current >= request_limit then
    return {2, request_current, redis.call('PTTL', KEYS[1]), 0, 0}
end

redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now_ms)
if redis.call('ZSCORE', KEYS[2], request_id) then
    return {5, request_current, 0, redis.call('ZCARD', KEYS[2]), 0}
end
local concurrency_current = tonumber(redis.call('ZCARD', KEYS[2]))
if concurrency_limit > 0 and concurrency_current >= concurrency_limit then
    return {3, request_current, 0, concurrency_current, 0}
end

local token_used = tonumber(redis.call('HGET', KEYS[3], 'used') or '0')
if token_limit > 0 then
    if redis.call('HEXISTS', KEYS[3], 'r:' .. request_id) == 1
       or redis.call('HEXISTS', KEYS[3], 's:' .. request_id) == 1 then
        return {5, request_current, 0, concurrency_current, token_used}
    end
    if token_used + estimated_tokens > token_limit then
        return {4, request_current, 0, concurrency_current, token_used}
    end
end

if request_limit > 0 then
    request_current = redis.call('INCR', KEYS[1])
    if request_current == 1 then redis.call('PEXPIRE', KEYS[1], request_ttl_ms) end
end
if concurrency_limit > 0 then
    concurrency_current = concurrency_current + 1
    redis.call('ZADD', KEYS[2], now_ms + lease_ms, request_id)
    redis.call('PEXPIRE', KEYS[2], lease_ms * 2)
end
if token_limit > 0 then
    token_used = redis.call('HINCRBY', KEYS[3], 'used', estimated_tokens)
    redis.call('HSET', KEYS[3], 'r:' .. request_id, estimated_tokens)
    redis.call('PEXPIRE', KEYS[3], token_ttl_ms)
end
return {1, request_current, request_ttl_ms, concurrency_current, token_used}
"""

_SETTLE_SCRIPT = """
local request_id = ARGV[1]
local actual_tokens = tonumber(ARGV[2])
local token_ttl_ms = tonumber(ARGV[3])
redis.call('ZREM', KEYS[1], request_id)
if redis.call('HEXISTS', KEYS[2], 's:' .. request_id) == 1 then
    return tonumber(redis.call('HGET', KEYS[2], 'used') or '0')
end
local reserved = redis.call('HGET', KEYS[2], 'r:' .. request_id)
if not reserved then return tonumber(redis.call('HGET', KEYS[2], 'used') or '0') end
reserved = tonumber(reserved)
if actual_tokens < 0 then actual_tokens = reserved end
local used = redis.call('HINCRBY', KEYS[2], 'used', actual_tokens - reserved)
redis.call('HDEL', KEYS[2], 'r:' .. request_id)
redis.call('HSET', KEYS[2], 's:' .. request_id, actual_tokens)
redis.call('PEXPIRE', KEYS[2], token_ttl_ms)
return used
"""

_RENEW_SCRIPT = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], 'XX', tonumber(ARGV[2]), ARGV[1])
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
    return 1
end
return 0
"""


class PolicyUnavailableError(Exception):
    pass


class PolicyRejectedError(Exception):
    def __init__(self, code: str, message: str, headers: dict[str, str]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.headers = headers


@dataclass
class PolicyLease:
    gateway_key: str
    request_id: str
    limits: PolicyLimits
    estimated_tokens: int
    token_day: str
    headers: dict[str, str]
    heartbeat: asyncio.Task[None] | None = field(default=None, repr=False)


class PolicyEngine:
    def __init__(self, redis: Redis, *, lease_seconds: int) -> None:
        self.redis = redis
        self.lease_seconds = lease_seconds

    async def admit(
        self,
        *,
        gateway_key: str,
        request_id: str,
        limits: PolicyLimits,
        estimated_tokens: int,
    ) -> PolicyLease:
        now = datetime.now(UTC)
        minute = now.strftime("%Y%m%d%H%M")
        day = now.strftime("%Y%m%d")
        tag = f"{{{gateway_key}}}"
        request_key = f"northgate:policy:{tag}:requests:{minute}"
        concurrency_key = f"northgate:policy:{tag}:concurrency"
        token_key = f"northgate:policy:{tag}:tokens:{day}"
        request_ttl_ms = max(1000, int((60 - now.second + 1) * 1000))
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        token_ttl_ms = int((tomorrow - now + timedelta(days=1)).total_seconds() * 1000)

        try:
            result = await self.redis.eval(
                _ADMIT_SCRIPT,
                3,
                request_key,
                concurrency_key,
                token_key,
                limits.requests_per_minute or 0,
                limits.concurrent_requests or 0,
                limits.tokens_per_day or 0,
                estimated_tokens,
                request_id,
                int(time.time() * 1000),
                self.lease_seconds * 1000,
                request_ttl_ms,
                token_ttl_ms,
            )
        except RedisError as exc:
            raise PolicyUnavailableError from exc

        code, request_current, request_ttl, concurrency_current, token_used = map(int, result)
        headers = _limit_headers(
            limits,
            request_current=request_current,
            request_ttl_ms=request_ttl,
            concurrency_current=concurrency_current,
            token_used=token_used,
        )
        if code == 2:
            raise PolicyRejectedError("REQUEST_LIMIT_EXCEEDED", "Request limit exceeded", headers)
        if code == 3:
            raise PolicyRejectedError(
                "CONCURRENCY_LIMIT_EXCEEDED", "Concurrency limit exceeded", headers
            )
        if code == 4:
            raise PolicyRejectedError("TOKEN_LIMIT_EXCEEDED", "Token limit exceeded", headers)
        if code == 5:
            raise PolicyRejectedError(
                "DUPLICATE_REQUEST_ID", "Request ID has already been used", headers
            )

        lease = PolicyLease(
            gateway_key=gateway_key,
            request_id=request_id,
            limits=limits,
            estimated_tokens=estimated_tokens,
            token_day=day,
            headers=headers,
        )
        if limits.concurrent_requests is not None:
            lease.heartbeat = asyncio.create_task(self._maintain(lease))
        return lease

    async def settle(self, lease: PolicyLease, actual_tokens: int | None) -> None:
        if lease.heartbeat is not None:
            lease.heartbeat.cancel()
            try:
                await lease.heartbeat
            except asyncio.CancelledError:
                pass

        now = datetime.now(UTC)
        tag = f"{{{lease.gateway_key}}}"
        concurrency_key = f"northgate:policy:{tag}:concurrency"
        token_key = f"northgate:policy:{tag}:tokens:{lease.token_day}"
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        token_ttl_ms = int((tomorrow - now + timedelta(days=1)).total_seconds() * 1000)
        try:
            await self.redis.eval(
                _SETTLE_SCRIPT,
                2,
                concurrency_key,
                token_key,
                lease.request_id,
                actual_tokens if actual_tokens is not None else -1,
                token_ttl_ms,
            )
        except RedisError:
            await logger.aexception("policy_settlement_failed", request_id=lease.request_id)

    async def _maintain(self, lease: PolicyLease) -> None:
        tag = f"{{{lease.gateway_key}}}"
        key = f"northgate:policy:{tag}:concurrency"
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            try:
                await self.redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    key,
                    lease.request_id,
                    int(time.time() * 1000) + self.lease_seconds * 1000,
                    self.lease_seconds * 2000,
                )
            except RedisError:
                await logger.aexception("concurrency_lease_renewal_failed")


def _limit_headers(
    limits: PolicyLimits,
    *,
    request_current: int,
    request_ttl_ms: int,
    concurrency_current: int,
    token_used: int,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if limits.requests_per_minute is not None:
        headers.update(
            {
                "Northgate-RateLimit-Limit": str(limits.requests_per_minute),
                "Northgate-RateLimit-Remaining": str(
                    max(0, limits.requests_per_minute - request_current)
                ),
                "Northgate-RateLimit-Reset": str(max(0, request_ttl_ms // 1000)),
            }
        )
    if limits.concurrent_requests is not None:
        headers["Northgate-ConcurrencyLimit-Remaining"] = str(
            max(0, limits.concurrent_requests - concurrency_current)
        )
    if limits.tokens_per_day is not None:
        headers["Northgate-TokenLimit-Remaining"] = str(max(0, limits.tokens_per_day - token_used))
    return headers
