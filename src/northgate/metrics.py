import asyncio
import time
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from sqlalchemy import func, select

from northgate.db.models import RequestRecord, SettlementEvent

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from northgate.db.database import Database
    from northgate.token_reservation import TokenReservation


class Metrics:
    def __init__(self, version: str) -> None:
        self.registry = CollectorRegistry()
        GCCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        ProcessCollector(registry=self.registry)

        self.build_info = Gauge(
            "northgate_build_info",
            "Northgate build information.",
            ("version",),
            registry=self.registry,
        )
        self.build_info.labels(version=version).set(1)
        self.http_requests = Counter(
            "northgate_http_requests_total",
            "HTTP requests completed by Northgate.",
            ("method", "route", "status_code"),
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "northgate_http_request_duration_seconds",
            "End-to-end HTTP request duration.",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
            registry=self.registry,
        )
        self.http_in_progress = Gauge(
            "northgate_http_requests_in_progress",
            "HTTP requests currently being handled.",
            registry=self.registry,
        )
        self.gateway_errors = Counter(
            "northgate_gateway_errors_total",
            "Stable gateway errors returned before or between provider attempts.",
            ("code",),
            registry=self.registry,
        )
        self.provider_attempts = Counter(
            "northgate_provider_attempts_total",
            "Provider calls completed by outcome.",
            ("provider", "adapter", "outcome"),
            registry=self.registry,
        )
        self.provider_attempt_duration = Histogram(
            "northgate_provider_attempt_duration_seconds",
            "Provider attempt duration.",
            ("provider", "adapter"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
            registry=self.registry,
        )
        self.provider_tokens = Counter(
            "northgate_provider_tokens_total",
            "Provider-reported tokens across completed attempts.",
            ("provider", "adapter", "type"),
            registry=self.registry,
        )
        self.provider_cost_microusd = Counter(
            "northgate_provider_cost_microusd_total",
            "Calculated provider cost in millionths of a US dollar.",
            ("provider", "adapter"),
            registry=self.registry,
        )
        self.token_reservation_tokens = Histogram(
            "northgate_token_reservation_tokens",
            "Token reservation components before provider forwarding.",
            ("component", "estimator", "output_source"),
            buckets=(16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576),
            registry=self.registry,
        )
        self.token_reservation_actual_ratio = Histogram(
            "northgate_token_reservation_actual_ratio",
            "Reserved total tokens divided by provider-reported actual total tokens.",
            buckets=(1, 1.25, 1.5, 2, 3, 5, 10, 25, 100),
            registry=self.registry,
        )
        self.token_reservation_released = Histogram(
            "northgate_token_reservation_released_tokens",
            "Reserved tokens released when provider-reported actual usage settles.",
            buckets=(0, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576),
            registry=self.registry,
        )
        self.cache_requests = Counter(
            "northgate_cache_requests_total",
            "Exact-cache request outcomes.",
            ("result",),
            registry=self.registry,
        )
        self.cache_writes = Counter(
            "northgate_cache_writes_total",
            "Exact-cache write outcomes.",
            ("result",),
            registry=self.registry,
        )
        self.route_skips = Counter(
            "northgate_route_skips_total",
            "Routes skipped without a provider call.",
            ("provider", "adapter", "reason"),
            registry=self.registry,
        )
        self.settlement_failures = Counter(
            "northgate_settlement_failures_total",
            "Settlement operations that failed and require retry or reconciliation.",
            ("stage",),
            registry=self.registry,
        )
        self.started_requests = Gauge(
            "northgate_started_requests",
            "Request records that have not reached a terminal outcome.",
            registry=self.registry,
        )
        self.oldest_started_request_age = Gauge(
            "northgate_oldest_started_request_age_seconds",
            "Age of the oldest request record without a terminal outcome.",
            registry=self.registry,
        )
        self.settlement_outbox_pending = Gauge(
            "northgate_settlement_outbox_pending_events",
            "Settlement events pending, retrying, or being processed.",
            registry=self.registry,
        )
        self.oldest_settlement_outbox_pending_age = Gauge(
            "northgate_oldest_settlement_outbox_pending_event_age_seconds",
            "Age of the oldest settlement event that has not completed or failed.",
            registry=self.registry,
        )
        self.settlement_outbox_failed = Gauge(
            "northgate_settlement_outbox_failed_events",
            "Settlement events that exhausted automatic retry attempts.",
            registry=self.registry,
        )
        self.settlement_worker_available = Gauge(
            "northgate_settlement_worker_available",
            "Whether at least one settlement worker heartbeat is present.",
            registry=self.registry,
        )
        self.active_concurrency_leases = Gauge(
            "northgate_active_concurrency_leases",
            "Active concurrency leases across gateway policy subjects.",
            registry=self.registry,
        )
        self.oldest_active_concurrency_lease_age = Gauge(
            "northgate_oldest_active_concurrency_lease_age_seconds",
            "Age of the oldest active concurrency lease with start metadata.",
            registry=self.registry,
        )
        self.operational_state_collection_failures = Counter(
            "northgate_operational_state_collection_failures_total",
            "Failures refreshing operational state metrics.",
            ("store",),
            registry=self.registry,
        )
        self.database_connection_invalidations = Counter(
            "northgate_database_connection_invalidations_total",
            "SQLAlchemy pool connection invalidations by explicit reason.",
            ("reason",),
            registry=self.registry,
        )

    def observe_database_connection_invalidation(self, exception: BaseException | None) -> None:
        reason = (
            "cancelled"
            if isinstance(exception, asyncio.CancelledError)
            else "error"
            if exception is not None
            else "unspecified"
        )
        self.database_connection_invalidations.labels(reason=reason).inc()

    async def refresh_operational_state(
        self,
        database: "Database | None",
        redis: "Redis | None",
    ) -> None:
        now = datetime.now(UTC)
        if database is not None:
            try:
                async with database.sessions() as session:
                    count, oldest = (
                        await session.execute(
                            select(func.count(), func.min(RequestRecord.started_at)).where(
                                RequestRecord.outcome == "started"
                            )
                        )
                    ).one()
                    pending_count, oldest_pending = (
                        await session.execute(
                            select(func.count(), func.min(SettlementEvent.created_at)).where(
                                SettlementEvent.status.in_(("pending", "retry", "processing"))
                            )
                        )
                    ).one()
                    failed_count = await session.scalar(
                        select(func.count()).where(SettlementEvent.status == "failed")
                    )
                self.started_requests.set(int(count))
                age = max(0.0, (now - oldest).total_seconds()) if oldest is not None else 0.0
                self.oldest_started_request_age.set(age)
                self.settlement_outbox_pending.set(int(pending_count))
                pending_age = (
                    max(0.0, (now - oldest_pending).total_seconds())
                    if oldest_pending is not None
                    else 0.0
                )
                self.oldest_settlement_outbox_pending_age.set(pending_age)
                self.settlement_outbox_failed.set(int(failed_count or 0))
            except Exception:
                self.operational_state_collection_failures.labels(store="postgresql").inc()

        if redis is not None:
            try:
                now_ms = int(now.timestamp() * 1000)
                active_count = 0
                oldest_started_ms: int | None = None
                async for key in redis.scan_iter(match="northgate:policy:*:concurrency"):
                    active_ids = await redis.zrangebyscore(key, now_ms + 1, "+inf")
                    active_count += len(active_ids)
                    if not active_ids:
                        continue
                    started_key = key + b":started" if isinstance(key, bytes) else f"{key}:started"
                    for value in await redis.hmget(started_key, active_ids):
                        if value is None:
                            continue
                        started_ms = int(value)
                        if oldest_started_ms is None or started_ms < oldest_started_ms:
                            oldest_started_ms = started_ms
                self.active_concurrency_leases.set(active_count)
                lease_age = (
                    max(0.0, (now_ms - oldest_started_ms) / 1000)
                    if oldest_started_ms is not None
                    else 0.0
                )
                self.oldest_active_concurrency_lease_age.set(lease_age)
                worker_available = False
                async for _key in redis.scan_iter(
                    match="northgate:settlement:worker:heartbeat:*", count=10
                ):
                    worker_available = True
                    break
                self.settlement_worker_available.set(1 if worker_available else 0)
            except Exception:
                self.operational_state_collection_failures.labels(store="redis").inc()

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        started_at: float,
    ) -> None:
        self.http_requests.labels(
            method=method,
            route=route,
            status_code=str(status_code),
        ).inc()
        self.http_request_duration.labels(method=method, route=route).observe(
            time.perf_counter() - started_at
        )

    def observe_provider_attempt(
        self,
        *,
        provider: str,
        adapter: str,
        outcome: str,
        duration_seconds: float,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cached_prompt_tokens: int | None,
        cost_microusd: int | None,
    ) -> None:
        self.provider_attempts.labels(
            provider=provider,
            adapter=adapter,
            outcome=outcome,
        ).inc()
        self.provider_attempt_duration.labels(provider=provider, adapter=adapter).observe(
            duration_seconds
        )
        if prompt_tokens is not None and prompt_tokens >= 0:
            self.provider_tokens.labels(provider=provider, adapter=adapter, type="prompt").inc(
                prompt_tokens
            )
        if completion_tokens is not None and completion_tokens >= 0:
            self.provider_tokens.labels(provider=provider, adapter=adapter, type="completion").inc(
                completion_tokens
            )
        if cached_prompt_tokens is not None and cached_prompt_tokens >= 0:
            self.provider_tokens.labels(
                provider=provider, adapter=adapter, type="cached_prompt"
            ).inc(cached_prompt_tokens)
        if cost_microusd is not None and cost_microusd >= 0:
            self.provider_cost_microusd.labels(provider=provider, adapter=adapter).inc(
                cost_microusd
            )

    def observe_token_reservation(self, reservation: "TokenReservation") -> None:
        labels = {
            "estimator": reservation.estimator,
            "output_source": reservation.output_limit_source,
        }
        for component, value in (
            ("prompt", reservation.estimated_prompt_tokens),
            ("output", reservation.reserved_output_tokens),
            ("margin", reservation.reservation_margin_tokens),
            ("total", reservation.reserved_total_tokens),
        ):
            self.token_reservation_tokens.labels(component=component, **labels).observe(value)

    def observe_token_settlement(self, reserved_tokens: int, actual_tokens: int | None) -> None:
        if actual_tokens is None or actual_tokens < 0:
            return
        self.token_reservation_released.observe(max(0, reserved_tokens - actual_tokens))
        if actual_tokens > 0:
            self.token_reservation_actual_ratio.observe(reserved_tokens / actual_tokens)


async def metrics_response(request: Request) -> Response:
    settings = request.app.state.settings
    expected = settings.metrics_key_sha256
    if expected is not None and expected.get_secret_value():
        scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
        actual = (
            sha256(credential.encode()).hexdigest()
            if separator and scheme.lower() == "bearer"
            else ""
        )
        if not compare_digest(actual, expected.get_secret_value()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    metrics: Metrics = request.app.state.metrics
    await metrics.refresh_operational_state(
        request.app.state.database,
        request.app.state.redis,
    )
    return Response(
        content=generate_latest(metrics.registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
