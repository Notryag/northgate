import base64
import binascii
import json
from dataclasses import dataclass
from hashlib import sha256

from redis.asyncio import Redis
from redis.exceptions import RedisError

from northgate.routing import ResolvedRoute


class CacheUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class CacheEntry:
    status_code: int
    headers: dict[str, str]
    body: bytes
    route_key: str


class ExactCache:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def get(self, key: str) -> CacheEntry | None:
        try:
            encoded = await self.redis.get(key)
        except RedisError as exc:
            raise CacheUnavailableError from exc
        if encoded is None:
            return None
        try:
            payload = json.loads(encoded)
            status_code = payload["status_code"]
            headers = payload["headers"]
            route_key = payload["route_key"]
            body = base64.b64decode(payload["body"], validate=True)
            if (
                not isinstance(status_code, int)
                or not 200 <= status_code < 300
                or not isinstance(headers, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                )
                or not isinstance(route_key, str)
            ):
                return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error):
            return None
        return CacheEntry(
            status_code=status_code,
            headers=headers,
            body=body,
            route_key=route_key,
        )

    async def set(self, key: str, entry: CacheEntry, ttl_seconds: int) -> None:
        encoded = json.dumps(
            {
                "status_code": entry.status_code,
                "headers": entry.headers,
                "body": base64.b64encode(entry.body).decode(),
                "route_key": entry.route_key,
            },
            separators=(",", ":"),
        )
        try:
            await self.redis.set(key, encoded, ex=ttl_seconds)
        except RedisError as exc:
            raise CacheUnavailableError from exc


def exact_cache_key(
    *,
    gateway_key: str,
    body: bytes,
    metadata: dict[str, str],
    request_headers: dict[str, str],
    routes: list[ResolvedRoute],
) -> str:
    route_configuration = sorted(
        (
            str(route.route_id or ""),
            route.provider,
            route.base_url.rstrip("/"),
            route.priority,
            route.weight,
            route.match_metadata,
        )
        for route in routes
    )
    digest = sha256()
    digest.update(b"northgate-exact-cache-v1\0")
    digest.update(gateway_key.encode())
    digest.update(b"\0")
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(json.dumps(request_headers, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(json.dumps(route_configuration, separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(body)
    gateway_digest = sha256(gateway_key.encode()).hexdigest()[:16]
    return f"northgate:cache:{{{gateway_digest}}}:{digest.hexdigest()}"
