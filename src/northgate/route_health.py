from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

_ALLOW_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local token = ARGV[2]
local probe_ttl_ms = tonumber(ARGV[3])
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
if open_until > now_ms then return 0 end
if open_until > 0 then
    local probe_until = tonumber(redis.call('HGET', KEYS[1], 'probe_until') or '0')
    if probe_until > now_ms then return 0 end
    redis.call('HSET', KEYS[1], 'probe_owner', token, 'probe_until', now_ms + probe_ttl_ms)
    redis.call('PEXPIRE', KEYS[1], probe_ttl_ms * 4)
    return 2
end
return 1
"""

_SUCCESS_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local token = ARGV[2]
local owner = redis.call('HGET', KEYS[1], 'probe_owner')
if owner then
    if owner == token then redis.call('DEL', KEYS[1]); return 1 end
    return 0
end
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
if open_until > now_ms then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""

_FAILURE_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local token = ARGV[2]
local threshold = tonumber(ARGV[3])
local recovery_ms = tonumber(ARGV[4])
local owner = redis.call('HGET', KEYS[1], 'probe_owner')
if owner and owner ~= token then
    return tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
end
redis.call('HDEL', KEYS[1], 'probe_owner', 'probe_until')
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if failures >= threshold then
    redis.call('HSET', KEYS[1], 'open_until', now_ms + recovery_ms)
end
redis.call('PEXPIRE', KEYS[1], recovery_ms * 4)
return failures
"""


class RouteHealthUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class RouteHealthDecision:
    allowed: bool
    probe: bool = False


class RouteHealthEngine:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def allow(
        self,
        *,
        route_key: str,
        token: str,
        now_ms: int,
        recovery_seconds: int,
    ) -> RouteHealthDecision:
        try:
            result = int(
                await self.redis.eval(
                    _ALLOW_SCRIPT,
                    1,
                    self._key(route_key),
                    now_ms,
                    token,
                    recovery_seconds * 1000,
                )
            )
        except RedisError as exc:
            raise RouteHealthUnavailableError from exc
        return RouteHealthDecision(allowed=result in (1, 2), probe=result == 2)

    async def record_success(self, *, route_key: str, token: str, now_ms: int) -> None:
        try:
            await self.redis.eval(_SUCCESS_SCRIPT, 1, self._key(route_key), now_ms, token)
        except RedisError as exc:
            raise RouteHealthUnavailableError from exc

    async def record_failure(
        self,
        *,
        route_key: str,
        token: str,
        now_ms: int,
        threshold: int,
        recovery_seconds: int,
    ) -> int:
        try:
            return int(
                await self.redis.eval(
                    _FAILURE_SCRIPT,
                    1,
                    self._key(route_key),
                    now_ms,
                    token,
                    threshold,
                    recovery_seconds * 1000,
                )
            )
        except RedisError as exc:
            raise RouteHealthUnavailableError from exc

    @staticmethod
    def _key(route_key: str) -> str:
        return f"northgate:route-health:{{{route_key}}}"
