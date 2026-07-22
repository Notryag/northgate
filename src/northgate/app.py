from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.sdk.trace.export import SpanExporter
from redis.asyncio import Redis

from northgate import __version__
from northgate.analytics import (
    usage_attempts,
    usage_requests,
    usage_routes,
    usage_summary,
    usage_tenants,
    usage_timeseries,
)
from northgate.config import Settings, get_settings
from northgate.console import console_index
from northgate.control import router as control_router
from northgate.credentials import CredentialCipher
from northgate.db.database import Database
from northgate.diagnostics import diagnostics_correlated, diagnostics_request, diagnostics_stale
from northgate.exact_cache import ExactCache
from northgate.logging import configure_logging
from northgate.metrics import Metrics, metrics_response
from northgate.middleware import RequestContextMiddleware
from northgate.policy import PolicyEngine
from northgate.pricing import PricingRepository
from northgate.proxy import proxy_chat_completions
from northgate.route_health import RouteHealthEngine
from northgate.routing import DatabaseRouteResolver
from northgate.settlement import (
    SettlementCoordinator,
    settlement_backlog,
    settlement_worker_available,
)
from northgate.tracing import Tracing
from northgate.usage import UsageRecorder


def create_app(
    settings: Settings | None = None,
    *,
    upstream_client: httpx.AsyncClient | None = None,
    database: Database | None = None,
    redis: Redis | None = None,
    span_exporter: SpanExporter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.settlement_outbox_enabled and not settings.usage_persistence_enabled:
        raise ValueError(
            "NORTHGATE_SETTLEMENT_OUTBOX_ENABLED requires NORTHGATE_USAGE_PERSISTENCE_ENABLED"
        )
    configure_logging(settings.log_level)
    metrics = Metrics(__version__) if settings.metrics_enabled else None
    database_required = settings.routing_source == "database" or settings.usage_persistence_enabled
    active_database = database
    owns_database = False
    if database_required and active_database is None:
        active_database = Database(settings.database_url.get_secret_value())
        owns_database = True
    if metrics is not None and isinstance(active_database, Database):
        active_database.add_invalidation_listener(metrics.observe_database_connection_invalidation)
    configured_policy = any(
        limit is not None
        for limit in (
            settings.request_limit_per_minute,
            settings.concurrency_limit,
            settings.token_limit_per_day,
            settings.daily_spend_limit_microusd,
            settings.monthly_spend_limit_microusd,
        )
    )
    redis_required = (
        settings.routing_source == "database"
        or configured_policy
        or settings.settlement_outbox_enabled
        or settings.route_health_enabled
        or settings.exact_cache_ttl_seconds is not None
    )
    active_redis = redis
    owns_redis = False
    if redis_required and active_redis is None:
        active_redis = Redis.from_url(settings.redis_url.get_secret_value())
        owns_redis = True

    encryption_key = settings.credential_encryption_key
    credential_cipher = (
        CredentialCipher(encryption_key.get_secret_value())
        if encryption_key is not None and encryption_key.get_secret_value()
        else None
    )
    route_resolver = None
    if settings.routing_source == "database":
        if credential_cipher is None:
            raise ValueError("NORTHGATE_CREDENTIAL_ENCRYPTION_KEY is required for database routing")
        if active_database is None:
            raise RuntimeError("Database routing requires a database")
        route_resolver = DatabaseRouteResolver(
            active_database,
            credential_cipher,
        )
    tracing = (
        Tracing(settings, __version__, span_exporter=span_exporter)
        if settings.tracing_enabled
        else None
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
            if tracing is not None:
                tracing.shutdown()

    app = FastAPI(
        title="Northgate",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.console_directory = settings.console_directory
    app.state.database = active_database
    app.state.redis = active_redis
    app.state.credential_cipher = credential_cipher
    app.state.route_resolver = route_resolver
    app.state.metrics = metrics
    app.state.tracing = tracing
    app.state.exact_cache = ExactCache(active_redis) if active_redis is not None else None
    app.state.route_health_engine = (
        RouteHealthEngine(active_redis) if active_redis is not None else None
    )
    app.state.policy_engine = (
        PolicyEngine(
            active_redis,
            lease_seconds=settings.concurrency_lease_seconds,
            metrics=metrics,
        )
        if active_redis is not None
        else None
    )
    app.state.pricing_repository = (
        PricingRepository(active_database) if active_database is not None else None
    )
    app.state.usage_recorder = (
        UsageRecorder(active_database)
        if settings.usage_persistence_enabled and active_database is not None
        else None
    )
    app.state.settlement_coordinator = (
        SettlementCoordinator(active_database, app.state.policy_engine)
        if settings.settlement_outbox_enabled and active_database is not None
        else None
    )
    if upstream_client is not None:
        app.state.upstream_client = upstream_client
    app.add_middleware(RequestContextMiddleware, metrics=metrics, tracing=tracing)

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
        if settings.settlement_outbox_enabled:
            if active_redis is None or not await settlement_worker_available(active_redis):
                if active_database is None:
                    return JSONResponse(
                        {"status": "not_ready", "reason": "settlement_backlog_unavailable"},
                        status_code=503,
                    )
                try:
                    backlog = await settlement_backlog(active_database)
                except Exception:
                    return JSONResponse(
                        {"status": "not_ready", "reason": "settlement_backlog_unavailable"},
                        status_code=503,
                    )
                backlog_details = {
                    "pending_events": backlog.pending_events,
                    "oldest_age_seconds": round(backlog.oldest_age_seconds, 3),
                }
                if (
                    backlog.pending_events == 0
                    or backlog.oldest_age_seconds
                    <= settings.settlement_readiness_max_pending_age_seconds
                ):
                    return JSONResponse(
                        {
                            "status": "ready",
                            "degraded": True,
                            "reason": "settlement_worker_unavailable",
                            "settlement_backlog": backlog_details,
                        }
                    )
                return JSONResponse(
                    {
                        "status": "not_ready",
                        "reason": "settlement_backlog_overdue",
                        "settlement_backlog": backlog_details,
                    },
                    status_code=503,
                )
        return JSONResponse({"status": "ready"})

    app.add_api_route(
        "/v1/gateways/{gateway_slug}/openai/chat/completions",
        proxy_chat_completions,
        methods=["POST"],
    )
    app.include_router(control_router)
    if metrics is not None:
        app.add_api_route("/metrics", metrics_response, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/v1/usage/summary", usage_summary, methods=["GET"])
    app.add_api_route("/api/v1/usage/timeseries", usage_timeseries, methods=["GET"])
    app.add_api_route("/api/v1/usage/routes", usage_routes, methods=["GET"])
    app.add_api_route("/api/v1/usage/tenants", usage_tenants, methods=["GET"])
    app.add_api_route("/api/v1/usage/requests", usage_requests, methods=["GET"])
    app.add_api_route(
        "/api/v1/usage/requests/{request_id}/attempts",
        usage_attempts,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/v1/diagnostics/requests/{request_id}",
        diagnostics_request,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/v1/diagnostics/correlated",
        diagnostics_correlated,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/v1/diagnostics/stale",
        diagnostics_stale,
        methods=["GET"],
    )
    app.mount(
        "/console/assets",
        StaticFiles(directory=settings.console_directory / "assets", check_dir=False),
        name="console-assets",
    )
    app.add_api_route("/console", console_index, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/console/{path:path}",
        console_index,
        methods=["GET"],
        include_in_schema=False,
    )

    return app


app = create_app()
