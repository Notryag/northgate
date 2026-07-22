import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import structlog

from northgate.exact_cache import CacheEntry, ExactCache
from northgate.metrics import Metrics
from northgate.policy import PolicyEngine, PolicyLease
from northgate.pricing import PriceQuote
from northgate.proxy_settlement import (
    AttemptTotals,
    settle_attempt_safely,
    settle_request_safely,
)
from northgate.route_health import RouteHealthEngine, record_route_health
from northgate.routing import ResolvedRoute
from northgate.settlement import SettlementCoordinator
from northgate.stream_relay import relay_response_body
from northgate.usage import UsageAccumulator, UsageRecorder

logger = structlog.get_logger()

_CACHED_RESPONSE_HEADERS = {"content-encoding", "content-type"}


def _cached_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() in _CACHED_RESPONSE_HEADERS
    }


@dataclass
class StreamFinalization:
    response: httpx.Response
    recorder: UsageRecorder | None
    request_id: str
    started_at: float
    policy_engine: PolicyEngine | None
    policy_lease: PolicyLease | None
    price: PriceQuote | None
    route: ResolvedRoute
    attempt_id: UUID | None
    attempt_started_at: float
    totals: AttemptTotals
    route_health_engine: RouteHealthEngine | None
    route_health_key: str
    route_health_token: str
    exact_cache: ExactCache | None
    cache_key: str | None
    cache_ttl_seconds: int | None
    metrics: Metrics | None
    settlement_coordinator: SettlementCoordinator | None

    async def finish(
        self,
        *,
        accumulator: UsageAccumulator,
        outcome: str,
        transport_failed: bool,
        completed: bool,
        cache_body: bytearray | None,
    ) -> None:
        if transport_failed:
            self.totals.ambiguous = True
        try:
            await self.response.aclose()
        except Exception:
            if self.metrics is not None:
                self.metrics.settlement_failures.labels(stage="upstream_close").inc()
            await logger.aexception(
                "upstream_close_failed",
                northgate_request_id=self.request_id,
            )
        if (
            completed
            and 200 <= self.response.status_code < 300
            and cache_body is not None
            and self.exact_cache is not None
            and self.cache_key is not None
            and self.cache_ttl_seconds is not None
        ):
            try:
                await self.exact_cache.set(
                    self.cache_key,
                    CacheEntry(
                        status_code=self.response.status_code,
                        headers=_cached_response_headers(self.response),
                        body=bytes(cache_body),
                        route_key=self.route_health_key,
                    ),
                    self.cache_ttl_seconds,
                )
                if self.metrics is not None:
                    self.metrics.cache_writes.labels(result="stored").inc()
            except Exception:
                if self.metrics is not None:
                    self.metrics.cache_writes.labels(result="error").inc()
                    self.metrics.settlement_failures.labels(stage="cache").inc()
                await logger.awarning(
                    "exact_cache_write_failed",
                    northgate_request_id=self.request_id,
                )
        elif (
            completed
            and 200 <= self.response.status_code < 300
            and self.cache_key is not None
            and cache_body is None
            and self.metrics is not None
        ):
            self.metrics.cache_writes.labels(result="oversized").inc()
        if outcome != "client_disconnected":
            health_failed = transport_failed or (
                self.response.status_code in self.route.health_failure_status_codes
            )
            try:
                await record_route_health(
                    self.route_health_engine,
                    self.route,
                    route_key=self.route_health_key,
                    token=self.route_health_token,
                    failed=health_failed,
                )
            except Exception:
                if self.metrics is not None:
                    self.metrics.settlement_failures.labels(stage="route_health").inc()
                await logger.aexception(
                    "route_health_settlement_failed",
                    route=self.route_health_key,
                    northgate_request_id=self.request_id,
                )
        usage = accumulator.result()
        if outcome == "client_disconnected" and usage.total_tokens is None:
            self.totals.ambiguous = True
        cost_microusd = (
            self.price.usage_cost(usage.prompt_tokens, usage.completion_tokens)
            if self.price is not None
            else None
        )
        self.totals.add(usage, cost_microusd)
        aggregate_usage = self.totals.usage()
        aggregate_cost = self.totals.cost_microusd if self.totals.has_cost else None
        if self.settlement_coordinator is not None and self.recorder is not None:
            payload: dict[str, object] = {
                "request": {
                    "outcome": outcome,
                    "status_code": self.response.status_code,
                    "provider_request_id": self.response.headers.get("x-request-id"),
                    "prompt_tokens": aggregate_usage.prompt_tokens,
                    "completion_tokens": aggregate_usage.completion_tokens,
                    "total_tokens": aggregate_usage.total_tokens,
                    "cached_prompt_tokens": aggregate_usage.cached_prompt_tokens,
                    "cost_microusd": aggregate_cost,
                    "route_id": str(self.route.route_id)
                    if self.route.route_id is not None
                    else None,
                    "provider": self.route.provider,
                    "price_id": (
                        str(self.price.price_id)
                        if self.price is not None and self.price.price_id is not None
                        else None
                    ),
                    "latency_ms": round((time.perf_counter() - self.started_at) * 1000),
                    "first_token_ms": accumulator.first_token_ms,
                },
                "attempt": (
                    {
                        "id": str(self.attempt_id),
                        "outcome": outcome,
                        "status_code": self.response.status_code,
                        "provider_request_id": self.response.headers.get("x-request-id"),
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                        "cached_prompt_tokens": usage.cached_prompt_tokens,
                        "cost_microusd": cost_microusd,
                        "latency_ms": round((time.perf_counter() - self.attempt_started_at) * 1000),
                    }
                    if self.attempt_id is not None
                    else None
                ),
                "policy": (
                    {
                        "gateway_key": self.policy_lease.gateway_key,
                        "token_day": self.policy_lease.token_day,
                        "spend_day": self.policy_lease.spend_day,
                        "spend_month": self.policy_lease.spend_month,
                        "actual_tokens": (
                            None if self.totals.ambiguous else aggregate_usage.total_tokens
                        ),
                        "actual_cost_microusd": (None if self.totals.ambiguous else aggregate_cost),
                    }
                    if self.policy_lease is not None
                    else None
                ),
            }
            try:
                event_id = await self.settlement_coordinator.enqueue(
                    request_id=self.request_id,
                    payload=payload,
                )
            except Exception:
                if self.metrics is not None:
                    self.metrics.settlement_failures.labels(stage="outbox_enqueue").inc()
                await logger.aexception(
                    "settlement_outbox_enqueue_failed",
                    northgate_request_id=self.request_id,
                )
            else:
                if self.policy_engine is not None and self.policy_lease is not None:
                    await self.policy_engine.stop_renewal(self.policy_lease)
                if await self.settlement_coordinator.process(event_id):
                    await settle_attempt_safely(
                        None,
                        metrics=self.metrics,
                        route=self.route,
                        attempt_id=self.attempt_id,
                        outcome=outcome,
                        status_code=self.response.status_code,
                        provider_request_id=self.response.headers.get("x-request-id"),
                        started_at=self.attempt_started_at,
                        usage=usage,
                        cost_microusd=cost_microusd,
                    )
                    return
        await settle_attempt_safely(
            self.recorder,
            metrics=self.metrics,
            route=self.route,
            attempt_id=self.attempt_id,
            outcome=outcome,
            status_code=self.response.status_code,
            provider_request_id=self.response.headers.get("x-request-id"),
            started_at=self.attempt_started_at,
            usage=usage,
            cost_microusd=cost_microusd,
        )
        if self.recorder is not None:
            await settle_request_safely(
                self.recorder,
                metrics=self.metrics,
                request_id=self.request_id,
                outcome=outcome,
                status_code=self.response.status_code,
                provider_request_id=self.response.headers.get("x-request-id"),
                started_at=self.started_at,
                usage=aggregate_usage,
                cost_microusd=aggregate_cost,
                first_token_ms=accumulator.first_token_ms,
                final_route=self.route,
                price_id=self.price.price_id if self.price is not None else None,
            )
        if self.policy_engine is not None and self.policy_lease is not None:
            await self.policy_engine.settle(
                self.policy_lease,
                None if self.totals.ambiguous else aggregate_usage.total_tokens,
                None if self.totals.ambiguous else aggregate_cost,
            )


def stream_response_body(
    response: httpx.Response,
    *,
    recorder: UsageRecorder | None,
    request_id: str,
    started_at: float,
    policy_engine: PolicyEngine | None,
    policy_lease: PolicyLease | None,
    price: PriceQuote | None,
    route: ResolvedRoute,
    attempt_id: UUID | None,
    attempt_started_at: float,
    totals: AttemptTotals,
    route_health_engine: RouteHealthEngine | None,
    route_health_key: str,
    route_health_token: str,
    exact_cache: ExactCache | None,
    cache_key: str | None,
    cache_ttl_seconds: int | None,
    cache_max_entry_bytes: int,
    metrics: Metrics | None,
    settlement_coordinator: SettlementCoordinator | None,
) -> AsyncIterator[bytes]:
    finalization = StreamFinalization(
        response=response,
        recorder=recorder,
        request_id=request_id,
        started_at=started_at,
        policy_engine=policy_engine,
        policy_lease=policy_lease,
        price=price,
        route=route,
        attempt_id=attempt_id,
        attempt_started_at=attempt_started_at,
        totals=totals,
        route_health_engine=route_health_engine,
        route_health_key=route_health_key,
        route_health_token=route_health_token,
        exact_cache=exact_cache,
        cache_key=cache_key,
        cache_ttl_seconds=cache_ttl_seconds,
        metrics=metrics,
        settlement_coordinator=settlement_coordinator,
    )
    return relay_response_body(
        response,
        started_at=started_at,
        cache_enabled=cache_key is not None,
        cache_max_entry_bytes=cache_max_entry_bytes,
        finalizer=finalization,
    )
