from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from northgate import __version__
from northgate.config import Settings, get_settings
from northgate.credentials import CredentialCipher
from northgate.db.database import Database
from northgate.logging import configure_logging
from northgate.middleware import RequestContextMiddleware
from northgate.policy import PolicyEngine
from northgate.proxy import proxy_chat_completions
from northgate.routing import DatabaseRouteResolver
from northgate.usage import UsageRecorder


def create_app(
    settings: Settings | None = None,
    *,
    upstream_client: httpx.AsyncClient | None = None,
    database: Database | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    database_required = settings.routing_source == "database" or settings.usage_persistence_enabled
    active_database = database
    owns_database = False
    if database_required and active_database is None:
        active_database = Database(settings.database_url.get_secret_value())
        owns_database = True
    configured_policy = any(
        limit is not None
        for limit in (
            settings.request_limit_per_minute,
            settings.concurrency_limit,
            settings.token_limit_per_day,
        )
    )
    policy_possible = settings.routing_source == "database" or configured_policy
    active_redis = redis
    owns_redis = False
    if policy_possible and active_redis is None:
        active_redis = Redis.from_url(settings.redis_url.get_secret_value())
        owns_redis = True

    route_resolver = None
    if settings.routing_source == "database":
        encryption_key = settings.credential_encryption_key
        if encryption_key is None or not encryption_key.get_secret_value():
            raise ValueError("NORTHGATE_CREDENTIAL_ENCRYPTION_KEY is required for database routing")
        if active_database is None:
            raise RuntimeError("Database routing requires a database")
        route_resolver = DatabaseRouteResolver(
            active_database,
            CredentialCipher(encryption_key.get_secret_value()),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if upstream_client is not None:
                app.state.upstream_client = upstream_client
                yield
                return

            timeout = httpx.Timeout(
                connect=settings.provider_connect_timeout_seconds,
                read=settings.provider_read_timeout_seconds,
                write=settings.provider_write_timeout_seconds,
                pool=settings.provider_pool_timeout_seconds,
            )
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                app.state.upstream_client = client
                yield
        finally:
            if owns_database and active_database is not None:
                await active_database.close()
            if owns_redis and active_redis is not None:
                await active_redis.aclose()

    app = FastAPI(
        title="Northgate",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.database = active_database
    app.state.route_resolver = route_resolver
    app.state.policy_engine = (
        PolicyEngine(active_redis, lease_seconds=settings.concurrency_lease_seconds)
        if active_redis is not None
        else None
    )
    app.state.usage_recorder = (
        UsageRecorder(active_database)
        if settings.usage_persistence_enabled and active_database is not None
        else None
    )
    if upstream_client is not None:
        app.state.upstream_client = upstream_client
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def readiness() -> JSONResponse:
        if active_database is not None and not await active_database.ping():
            return JSONResponse({"status": "not_ready"}, status_code=503)
        if active_redis is not None:
            try:
                await active_redis.ping()
            except Exception:
                return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.add_api_route(
        "/v1/gateways/{gateway_slug}/openai/chat/completions",
        proxy_chat_completions,
        methods=["POST"],
    )

    return app


app = create_app()
