import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import httpx
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from northgate.attempt_execution import execute_provider_attempt, resolve_retryable_response
from northgate.config import Settings
from northgate.exact_cache import (
    CacheEntry,
    CacheUnavailableError,
    ExactCache,
    exact_cache_key,
)
from northgate.metrics import Metrics
from northgate.policy import (
    PolicyEngine,
    PolicyLease,
    PolicyRejectedError,
    PolicyUnavailableError,
)
from northgate.pricing import PriceQuote, PricingRepository, configured_price
from northgate.provider_adapters import (
    AdapterRequestError,
    AdapterUnavailableError,
    provider_adapter,
)
from northgate.proxy_input import (
    RequestBodyTooLargeError,
    read_proxy_request_input,
)
from northgate.proxy_input import (
    estimated_tokens as estimate_request_tokens,
)
from northgate.proxy_input import (
    request_metadata as parse_request_metadata,
)
from northgate.route_health import RouteHealthEngine, RouteHealthUnavailableError
from northgate.route_planning import (
    plan_routes,
    resolve_routes,
    validate_primary_route,
)
from northgate.routing import (
    ForbiddenGatewayError,
    InvalidApplicationKeyError,
    ResolvedRoute,
    RouteUnavailableError,
)
from northgate.settlement import SettlementCoordinator
from northgate.stream_relay import relay_response_body
from northgate.tracing import Tracing, add_span_event
from northgate.usage import (
    DuplicateRequestError,
    UsageAccumulator,
    UsageRecorder,
    UsageResult,
)

logger = structlog.get_logger()

_FORWARDED_RESPONSE_HEADERS = {
    "cache-control",
    "content-encoding",
    "content-type",
    "retry-after",
    "x-request-id",
}
_CACHED_RESPONSE_HEADERS = {"content-encoding", "content-type"}


def _error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    metrics: Metrics | None = request.app.state.metrics
    if metrics is not None:
        metrics.gateway_errors.labels(code=code).inc()
    add_span_event(
        "northgate.gateway_error",
        {
            "northgate.error.code": code,
            "http.response.status_code": status_code,
            "northgate.error.retryable": retryable,
        },
    )
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request.state.request_id,
                "retryable": retryable,
            }
        },
    )


def _downstream_headers(
    response: httpx.Response,
    route: ResolvedRoute,
    policy_headers: dict[str, str],
    attempt_count: int,
    cache_status: str | None = None,
) -> dict[str, str]:
    headers = _forwarded_response_headers(response)
    headers["Northgate-Provider"] = route.provider
    headers["Northgate-Route"] = _route_label(route)
    headers["Northgate-Attempts"] = str(attempt_count)
    if cache_status is not None:
        headers["Northgate-Cache"] = cache_status
    headers.update(policy_headers)
    return headers


def _forwarded_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() in _FORWARDED_RESPONSE_HEADERS
        or name.lower().startswith(("openai-", "x-ratelimit-"))
    }


def _cached_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() in _CACHED_RESPONSE_HEADERS
    }


def _route_label(route: ResolvedRoute) -> str:
    return str(route.route_id) if route.route_id is not None else f"configured-{route.provider}"


async def _settle_safely(
    recorder: UsageRecorder,
    *,
    metrics: Metrics | None,
    request_id: str,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
    first_token_ms: int | None,
    final_route: ResolvedRoute,
    price_id: UUID | None,
) -> None:
    try:
        await recorder.settle(
            request_id=request_id,
            outcome=outcome,
            status_code=status_code,
            provider_request_id=provider_request_id,
            latency_ms=round((time.perf_counter() - started_at) * 1000),
            first_token_ms=first_token_ms,
            usage=usage,
            cost_microusd=cost_microusd,
            final_route=final_route,
            price_id=price_id,
        )
    except Exception:
        if metrics is not None:
            metrics.settlement_failures.labels(stage="request").inc()
        await logger.aexception("usage_settlement_failed", northgate_request_id=request_id)


async def _enqueue_request_settlement(
    coordinator: SettlementCoordinator | None,
    *,
    policy_engine: PolicyEngine | None,
    policy_lease: PolicyLease | None,
    metrics: Metrics | None,
    request_id: str,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
    first_token_ms: int | None,
    final_route: ResolvedRoute,
    price_id: UUID | None,
    attempt_id: UUID | None = None,
    attempt_started_at: float | None = None,
    attempt_outcome: str | None = None,
    attempt_usage: UsageResult | None = None,
    attempt_cost_microusd: int | None = None,
) -> bool:
    if coordinator is None:
        return False
    payload: dict[str, object] = {
        "request": {
            "outcome": outcome,
            "status_code": status_code,
            "provider_request_id": provider_request_id,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_prompt_tokens": usage.cached_prompt_tokens,
            "cost_microusd": cost_microusd,
            "route_id": str(final_route.route_id) if final_route.route_id is not None else None,
            "provider": final_route.provider,
            "price_id": str(price_id) if price_id is not None else None,
            "latency_ms": round((time.perf_counter() - started_at) * 1000),
            "first_token_ms": first_token_ms,
        },
        "attempt": (
            {
                "id": str(attempt_id),
                "outcome": attempt_outcome or outcome,
                "status_code": status_code,
                "provider_request_id": provider_request_id,
                "prompt_tokens": (attempt_usage or usage).prompt_tokens,
                "completion_tokens": (attempt_usage or usage).completion_tokens,
                "total_tokens": (attempt_usage or usage).total_tokens,
                "cached_prompt_tokens": (attempt_usage or usage).cached_prompt_tokens,
                "cost_microusd": attempt_cost_microusd,
                "latency_ms": round(
                    (time.perf_counter() - (attempt_started_at or started_at)) * 1000
                ),
            }
            if attempt_id is not None
            else None
        ),
        "policy": (
            {
                "gateway_key": policy_lease.gateway_key,
                "token_day": policy_lease.token_day,
                "spend_day": policy_lease.spend_day,
                "spend_month": policy_lease.spend_month,
                "actual_tokens": usage.total_tokens,
                "actual_cost_microusd": cost_microusd,
            }
            if policy_lease is not None
            else None
        ),
    }
    try:
        event_id = await coordinator.enqueue(request_id=request_id, payload=payload)
    except Exception:
        if metrics is not None:
            metrics.settlement_failures.labels(stage="outbox_enqueue").inc()
        await logger.aexception(
            "settlement_outbox_enqueue_failed",
            northgate_request_id=request_id,
        )
        return False
    if policy_engine is not None and policy_lease is not None:
        await policy_engine.stop_renewal(policy_lease)
    return await coordinator.process(event_id)


@dataclass
class AttemptTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_microusd: int = 0
    has_usage: bool = False
    has_cost: bool = False
    ambiguous: bool = False

    def add(self, usage: UsageResult, cost_microusd: int | None) -> None:
        if usage.total_tokens is not None:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0
            self.total_tokens += usage.total_tokens
            self.cached_prompt_tokens += usage.cached_prompt_tokens or 0
            self.has_usage = True
        if cost_microusd is not None:
            self.cost_microusd += cost_microusd
            self.has_cost = True

    def usage(self) -> UsageResult:
        if not self.has_usage:
            return UsageResult()
        return UsageResult(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            cached_prompt_tokens=self.cached_prompt_tokens,
        )


async def _settle_attempt_safely(
    recorder: UsageRecorder | None,
    *,
    metrics: Metrics | None,
    route: ResolvedRoute,
    attempt_id: UUID | None,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
) -> None:
    duration_seconds = time.perf_counter() - started_at
    if metrics is not None:
        metrics.observe_provider_attempt(
            provider=route.provider,
            adapter=route.adapter,
            outcome=outcome,
            duration_seconds=duration_seconds,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cost_microusd=cost_microusd,
        )
    add_span_event(
        "northgate.provider_attempt",
        {
            "northgate.provider": route.provider,
            "northgate.adapter": route.adapter,
            "northgate.attempt.outcome": outcome,
            "northgate.attempt.duration_ms": round(duration_seconds * 1000, 2),
            "northgate.usage.prompt_tokens": usage.prompt_tokens,
            "northgate.usage.completion_tokens": usage.completion_tokens,
            "northgate.usage.cached_prompt_tokens": usage.cached_prompt_tokens,
            "northgate.cost_microusd": cost_microusd,
            "http.response.status_code": status_code,
        },
    )
    if recorder is None or attempt_id is None:
        return
    try:
        await recorder.settle_attempt(
            attempt_id=attempt_id,
            outcome=outcome,
            status_code=status_code,
            provider_request_id=provider_request_id,
            latency_ms=round((time.perf_counter() - started_at) * 1000),
            usage=usage,
            cost_microusd=cost_microusd,
        )
    except Exception:
        if metrics is not None:
            metrics.settlement_failures.labels(stage="attempt").inc()
        await logger.aexception("provider_attempt_settlement_failed", attempt_id=str(attempt_id))


async def _settle_attempt_with_outbox(
    coordinator: SettlementCoordinator | None,
    recorder: UsageRecorder | None,
    *,
    metrics: Metrics | None,
    route: ResolvedRoute,
    request_id: str,
    attempt_id: UUID | None,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
) -> None:
    if coordinator is not None and recorder is not None and attempt_id is not None:
        payload: dict[str, object] = {
            "request": None,
            "attempt": {
                "id": str(attempt_id),
                "outcome": outcome,
                "status_code": status_code,
                "provider_request_id": provider_request_id,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cost_microusd": cost_microusd,
                "latency_ms": round((time.perf_counter() - started_at) * 1000),
            },
            "policy": None,
        }
        try:
            event_id = await coordinator.enqueue(
                request_id=request_id,
                event_key=f"attempt:{attempt_id}",
                payload=payload,
            )
        except Exception:
            if metrics is not None:
                metrics.settlement_failures.labels(stage="outbox_enqueue").inc()
            await logger.aexception(
                "attempt_settlement_outbox_enqueue_failed",
                northgate_request_id=request_id,
                attempt_id=str(attempt_id),
            )
        else:
            await _settle_attempt_safely(
                None,
                metrics=metrics,
                route=route,
                attempt_id=attempt_id,
                outcome=outcome,
                status_code=status_code,
                provider_request_id=provider_request_id,
                started_at=started_at,
                usage=usage,
                cost_microusd=cost_microusd,
            )
            if await coordinator.process(event_id):
                return
    await _settle_attempt_safely(
        recorder,
        metrics=metrics,
        route=route,
        attempt_id=attempt_id,
        outcome=outcome,
        status_code=status_code,
        provider_request_id=provider_request_id,
        started_at=started_at,
        usage=usage,
        cost_microusd=cost_microusd,
    )


def _route_health_key(route: ResolvedRoute) -> str:
    identity = (
        f"{route.route_id or ''}\0{route.provider}\0{route.base_url.rstrip('/')}\0"
        f"{route.adapter}\0{route.adapter_config}"
    )
    return sha256(identity.encode()).hexdigest()


async def _record_route_health(
    engine: RouteHealthEngine | None,
    route: ResolvedRoute,
    *,
    route_key: str,
    token: str,
    failed: bool,
) -> None:
    if engine is None or route.health_failure_threshold <= 0:
        return
    now_ms = int(time.time() * 1000)
    if failed:
        await engine.record_failure(
            route_key=route_key,
            token=token,
            now_ms=now_ms,
            threshold=route.health_failure_threshold,
            recovery_seconds=route.health_recovery_seconds,
        )
    else:
        await engine.record_success(route_key=route_key, token=token, now_ms=now_ms)


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
                await _record_route_health(
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
                    await _settle_attempt_safely(
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
        await _settle_attempt_safely(
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
            await _settle_safely(
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


def _response_body(
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


async def proxy_chat_completions(
    request: Request,
    gateway_slug: str,
) -> Response:
    settings: Settings = request.app.state.settings
    started_at = time.perf_counter()
    request_id: str = request.state.request_id

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            request_too_large = int(content_length) > settings.max_request_body_bytes
        except ValueError:
            request_too_large = False
        if request_too_large:
            return _error(
                request,
                status_code=413,
                code="REQUEST_TOO_LARGE",
                message="Request body is too large",
                retryable=False,
            )

    try:
        routes = await resolve_routes(request, gateway_slug, settings)
    except InvalidApplicationKeyError:
        return _error(
            request,
            status_code=401,
            code="INVALID_APPLICATION_KEY",
            message="Invalid application key",
            retryable=False,
        )

    except ForbiddenGatewayError:
        return _error(
            request,
            status_code=403,
            code="FORBIDDEN_GATEWAY",
            message="Application key cannot access this gateway",
            retryable=False,
        )

    except RouteUnavailableError:
        await logger.aerror("provider_credential_unavailable", gateway=gateway_slug)
        return _error(
            request,
            status_code=503,
            code="PROVIDER_UNAVAILABLE",
            message="Provider is unavailable",
            retryable=True,
        )

    route = routes[0]
    request_metadata = parse_request_metadata(request, route)
    if request_metadata is None:
        return _error(
            request,
            status_code=400,
            code="INVALID_METADATA",
            message="Invalid request metadata",
            retryable=False,
        )
    routes = plan_routes(routes, request_metadata, request_id)
    if not routes:
        return _error(
            request,
            status_code=503,
            code="ROUTE_NOT_MATCHED",
            message="No route matches the request metadata",
            retryable=False,
        )
    route = routes[0]

    try:
        proxy_input = await read_proxy_request_input(
            request,
            metadata=request_metadata,
            max_body_bytes=settings.max_request_body_bytes,
        )
    except RequestBodyTooLargeError:
        return _error(
            request,
            status_code=413,
            code="REQUEST_TOO_LARGE",
            message="Request body is too large",
            retryable=False,
        )
    body = proxy_input.body
    model = proxy_input.model
    try:
        validate_primary_route(route, model)
    except AdapterRequestError as exc:
        return _error(
            request,
            status_code=400,
            code="INVALID_PROVIDER_REQUEST",
            message=str(exc),
            retryable=False,
        )
    except AdapterUnavailableError:
        await logger.aexception(
            "provider_adapter_unavailable",
            northgate_request_id=request_id,
        )
        return _error(
            request,
            status_code=503,
            code="PROVIDER_ADAPTER_UNAVAILABLE",
            message="Provider adapter is unavailable",
            retryable=False,
        )

    forwarded_request_headers = proxy_input.forwarded_headers
    cache_request_headers = dict(forwarded_request_headers)
    policy_engine: PolicyEngine | None = request.app.state.policy_engine
    recorder: UsageRecorder | None = request.app.state.usage_recorder
    metrics: Metrics | None = request.app.state.metrics
    tracing: Tracing | None = request.app.state.tracing
    if tracing is not None:
        tracing.inject(forwarded_request_headers)
    exact_cache: ExactCache | None = request.app.state.exact_cache
    cache_key: str | None = None
    cache_entry: CacheEntry | None = None
    cache_read_error = False
    if route.exact_cache_ttl_seconds is not None and exact_cache is not None:
        cache_key = exact_cache_key(
            gateway_key=str(route.gateway_id or gateway_slug),
            body=body,
            metadata=request_metadata,
            request_headers=cache_request_headers,
            routes=routes,
        )
        try:
            cache_entry = await exact_cache.get(cache_key)
        except CacheUnavailableError:
            cache_read_error = True
            await logger.awarning(
                "exact_cache_read_failed",
                northgate_request_id=request_id,
            )

    cached_route = None
    if cache_entry is not None:
        cached_route = next(
            (
                candidate
                for candidate in routes
                if _route_health_key(candidate) == cache_entry.route_key
            ),
            None,
        )

    if route.exact_cache_ttl_seconds is None:
        cache_result = "bypass"
    elif cache_read_error or exact_cache is None:
        cache_result = "error"
    elif cache_entry is not None and cached_route is not None:
        cache_result = "hit"
    else:
        cache_result = "miss"
    if metrics is not None:
        metrics.cache_requests.labels(result=cache_result).inc()
    add_span_event("northgate.cache", {"northgate.cache.result": cache_result})

    if cache_entry is not None and cached_route is not None:
        cache_policy_lease: PolicyLease | None = None
        if route.policy.enabled:
            if policy_engine is None:
                return _error(
                    request,
                    status_code=503,
                    code="POLICY_UNAVAILABLE",
                    message="Request policy is unavailable",
                    retryable=True,
                )
            try:
                cache_policy_lease = await policy_engine.admit(
                    gateway_key=str(route.gateway_id or gateway_slug),
                    request_id=request_id,
                    limits=route.policy,
                    estimated_tokens=0,
                    estimated_cost_microusd=0,
                )
            except PolicyRejectedError as exc:
                return _error(
                    request,
                    status_code=429,
                    code=exc.code,
                    message=exc.message,
                    retryable=True,
                    headers=exc.headers,
                )
            except PolicyUnavailableError:
                return _error(
                    request,
                    status_code=503,
                    code="POLICY_UNAVAILABLE",
                    message="Request policy is unavailable",
                    retryable=True,
                )
        if recorder is not None:
            try:
                await recorder.start(
                    request_id=request_id,
                    route=cached_route,
                    model=model,
                    request_metadata=request_metadata,
                    price_id=None,
                    estimated_tokens=0,
                    cache_status="hit",
                )
            except DuplicateRequestError:
                if policy_engine is not None and cache_policy_lease is not None:
                    await policy_engine.settle(cache_policy_lease, 0, 0)
                return _error(
                    request,
                    status_code=409,
                    code="DUPLICATE_REQUEST_ID",
                    message="Request ID has already been used",
                    retryable=False,
                )
            except Exception:
                if policy_engine is not None and cache_policy_lease is not None:
                    await policy_engine.settle(cache_policy_lease, 0, 0)
                await logger.aexception("usage_record_start_failed")
                return _error(
                    request,
                    status_code=503,
                    code="INTERNAL_FAILURE",
                    message="Request accounting is unavailable",
                    retryable=True,
                )
            cache_usage = UsageResult(prompt_tokens=0, completion_tokens=0, total_tokens=0)
            durable_cache_settlement = await _enqueue_request_settlement(
                request.app.state.settlement_coordinator,
                policy_engine=policy_engine,
                policy_lease=cache_policy_lease,
                metrics=metrics,
                request_id=request_id,
                outcome="cache_hit",
                status_code=cache_entry.status_code,
                provider_request_id=None,
                started_at=started_at,
                usage=cache_usage,
                cost_microusd=0,
                first_token_ms=round((time.perf_counter() - started_at) * 1000),
                final_route=cached_route,
                price_id=None,
            )
            if not durable_cache_settlement:
                await _settle_safely(
                    recorder,
                    metrics=metrics,
                    request_id=request_id,
                    outcome="cache_hit",
                    status_code=cache_entry.status_code,
                    provider_request_id=None,
                    started_at=started_at,
                    usage=cache_usage,
                    cost_microusd=0,
                    first_token_ms=round((time.perf_counter() - started_at) * 1000),
                    final_route=cached_route,
                    price_id=None,
                )
                if policy_engine is not None and cache_policy_lease is not None:
                    await policy_engine.settle(cache_policy_lease, 0, 0)
        cache_headers = dict(cache_entry.headers)
        cache_headers.update(
            {
                "Northgate-Provider": cached_route.provider,
                "Northgate-Route": _route_label(cached_route),
                "Northgate-Attempts": "0",
                "Northgate-Cache": "HIT",
            }
        )
        if cache_policy_lease is not None:
            cache_headers.update(cache_policy_lease.headers)
        return Response(
            content=cache_entry.body,
            status_code=cache_entry.status_code,
            headers=cache_headers,
        )

    pricing_repository: PricingRepository | None = request.app.state.pricing_repository
    if settings.routing_source == "database" and pricing_repository is not None and model:
        prices = [
            await pricing_repository.resolve(candidate.provider, model, datetime.now(UTC))
            for candidate in routes
        ]
    else:
        prices = [configured_price(settings) for _ in routes]
    spend_limited = (
        route.policy.daily_spend_microusd is not None
        or route.policy.monthly_spend_microusd is not None
    )
    if spend_limited and any(price is None for price in prices):
        return _error(
            request,
            status_code=503,
            code="PRICING_UNAVAILABLE",
            message="Model pricing is unavailable",
            retryable=False,
        )

    estimated_input_tokens, estimated_output_tokens = estimate_request_tokens(body, settings)
    possible_attempts = sum(candidate.max_retries + 1 for candidate in routes)
    estimated_cost_microusd = sum(
        (price.cost(estimated_input_tokens, estimated_output_tokens) if price is not None else 0)
        * (candidate.max_retries + 1)
        for candidate, price in zip(routes, prices, strict=True)
    )
    policy_lease: PolicyLease | None = None
    if route.policy.enabled:
        if policy_engine is None:
            return _error(
                request,
                status_code=503,
                code="POLICY_UNAVAILABLE",
                message="Request policy is unavailable",
                retryable=True,
            )
        try:
            estimated_tokens = (
                estimated_input_tokens + estimated_output_tokens
            ) * possible_attempts
            policy_lease = await policy_engine.admit(
                gateway_key=str(route.gateway_id or gateway_slug),
                request_id=request_id,
                limits=route.policy,
                estimated_tokens=estimated_tokens,
                estimated_cost_microusd=estimated_cost_microusd,
            )
        except PolicyRejectedError as exc:
            if recorder is not None:
                try:
                    await recorder.record_rejection(
                        request_id=request_id,
                        route=route,
                        model=model,
                        request_metadata=request_metadata,
                        price_id=prices[0].price_id if prices[0] is not None else None,
                        estimated_tokens=estimated_tokens,
                        cache_status=cache_result,
                        error_code=exc.code,
                        status_code=429,
                    )
                except DuplicateRequestError:
                    pass
                except Exception:
                    await logger.aexception(
                        "policy_rejection_record_failed",
                        northgate_request_id=request_id,
                    )
            return _error(
                request,
                status_code=429,
                code=exc.code,
                message=exc.message,
                retryable=True,
                headers=exc.headers,
            )
        except PolicyUnavailableError:
            return _error(
                request,
                status_code=503,
                code="POLICY_UNAVAILABLE",
                message="Request policy is unavailable",
                retryable=True,
            )

    price = prices[0]
    if recorder is not None:
        try:
            await recorder.start(
                request_id=request_id,
                route=route,
                model=model,
                request_metadata=request_metadata,
                price_id=price.price_id if price is not None else None,
                estimated_tokens=(estimated_input_tokens + estimated_output_tokens)
                * possible_attempts,
                cache_status=cache_result,
            )
        except DuplicateRequestError:
            if policy_engine is not None and policy_lease is not None:
                await policy_engine.settle(policy_lease, 0, 0)
            return _error(
                request,
                status_code=409,
                code="DUPLICATE_REQUEST_ID",
                message="Request ID has already been used",
                retryable=False,
            )
        except Exception:
            if policy_engine is not None and policy_lease is not None:
                await policy_engine.settle(policy_lease, 0, 0)
            await logger.aexception("usage_record_start_failed")
            return _error(
                request,
                status_code=503,
                code="INTERNAL_FAILURE",
                message="Request accounting is unavailable",
                retryable=True,
            )

    attempt_plan = [
        (candidate, candidate_price)
        for candidate, candidate_price in zip(routes, prices, strict=True)
        for _ in range(candidate.max_retries + 1)
    ]
    totals = AttemptTotals()
    client: httpx.AsyncClient = request.app.state.upstream_client
    route_health_engine: RouteHealthEngine | None = request.app.state.route_health_engine
    last_route = route
    last_price = price
    last_failure = "provider_unavailable"
    provider_attempt_count = 0
    invalid_routes: set[str] = set()

    for plan_index, (candidate, candidate_price) in enumerate(attempt_plan):
        has_next = plan_index + 1 < len(attempt_plan)
        health_route_key = _route_health_key(candidate)
        health_token = f"{request_id}:{plan_index + 1}"
        if health_route_key in invalid_routes:
            continue
        try:
            provider_adapter(candidate.adapter).validate(candidate, model)
        except (AdapterRequestError, AdapterUnavailableError):
            # The selected primary is validated before admission. A bad fallback
            # is isolated here so it cannot block an otherwise healthy primary.
            invalid_routes.add(health_route_key)
            last_route = candidate
            last_price = candidate_price
            last_failure = "provider_unavailable"
            if metrics is not None:
                metrics.route_skips.labels(
                    provider=candidate.provider,
                    adapter=candidate.adapter,
                    reason="invalid_configuration",
                ).inc()
            await logger.aexception(
                "provider_route_invalid",
                route=health_route_key,
                northgate_request_id=request_id,
            )
            continue
        if candidate.health_failure_threshold > 0:
            if route_health_engine is None:
                health_error = True
            else:
                try:
                    decision = await route_health_engine.allow(
                        route_key=health_route_key,
                        token=health_token,
                        now_ms=int(time.time() * 1000),
                        recovery_seconds=candidate.health_recovery_seconds,
                    )
                    health_error = False
                except RouteHealthUnavailableError:
                    health_error = True
            if health_error:
                health_usage = totals.usage()
                health_cost = (
                    None if totals.ambiguous else totals.cost_microusd if totals.has_cost else None
                )
                durable_health_settlement = False
                if recorder is not None:
                    durable_health_settlement = await _enqueue_request_settlement(
                        request.app.state.settlement_coordinator,
                        policy_engine=policy_engine,
                        policy_lease=policy_lease,
                        metrics=metrics,
                        request_id=request_id,
                        outcome="route_health_unavailable",
                        status_code=503,
                        provider_request_id=None,
                        started_at=started_at,
                        usage=health_usage,
                        cost_microusd=health_cost,
                        first_token_ms=None,
                        final_route=candidate,
                        price_id=(
                            candidate_price.price_id if candidate_price is not None else None
                        ),
                    )
                    if not durable_health_settlement:
                        await _settle_safely(
                            recorder,
                            metrics=metrics,
                            request_id=request_id,
                            outcome="route_health_unavailable",
                            status_code=503,
                            provider_request_id=None,
                            started_at=started_at,
                            usage=health_usage,
                            cost_microusd=health_cost,
                            first_token_ms=None,
                            final_route=candidate,
                            price_id=(
                                candidate_price.price_id if candidate_price is not None else None
                            ),
                        )
                if (
                    not durable_health_settlement
                    and policy_engine is not None
                    and policy_lease is not None
                ):
                    await policy_engine.settle(policy_lease, health_usage.total_tokens, health_cost)
                return _error(
                    request,
                    status_code=503,
                    code="ROUTE_HEALTH_UNAVAILABLE",
                    message="Route health state is unavailable",
                    retryable=True,
                    headers=policy_lease.headers if policy_lease is not None else None,
                )
            if not decision.allowed:
                last_route = candidate
                last_price = candidate_price
                last_failure = "routes_unhealthy"
                if metrics is not None:
                    metrics.route_skips.labels(
                        provider=candidate.provider,
                        adapter=candidate.adapter,
                        reason="circuit_open",
                    ).inc()
                add_span_event(
                    "northgate.route_skipped",
                    {
                        "northgate.provider": candidate.provider,
                        "northgate.adapter": candidate.adapter,
                        "northgate.route_skip.reason": "circuit_open",
                    },
                )
                await logger.ainfo(
                    "provider_route_skipped",
                    route=health_route_key,
                    northgate_request_id=request_id,
                )
                continue

        provider_attempt_count += 1
        attempt_index = provider_attempt_count
        attempt_started_at = time.perf_counter()
        attempt_id: UUID | None = None
        if recorder is not None:
            try:
                attempt_id = await recorder.start_attempt(
                    request_id=request_id,
                    attempt_index=attempt_index,
                    route=candidate,
                    price_id=candidate_price.price_id if candidate_price is not None else None,
                )
            except Exception:
                await logger.aexception(
                    "provider_attempt_start_failed",
                    northgate_request_id=request_id,
                    attempt_index=attempt_index,
                )
                attempt_start_usage = totals.usage()
                attempt_start_cost = (
                    None if totals.ambiguous else totals.cost_microusd if totals.has_cost else None
                )
                durable_attempt_start_settlement = await _enqueue_request_settlement(
                    request.app.state.settlement_coordinator,
                    policy_engine=policy_engine,
                    policy_lease=policy_lease,
                    metrics=metrics,
                    request_id=request_id,
                    outcome="internal_failure",
                    status_code=503,
                    provider_request_id=None,
                    started_at=started_at,
                    usage=attempt_start_usage,
                    cost_microusd=attempt_start_cost,
                    first_token_ms=None,
                    final_route=candidate,
                    price_id=candidate_price.price_id if candidate_price is not None else None,
                )
                if not durable_attempt_start_settlement:
                    await _settle_safely(
                        recorder,
                        metrics=metrics,
                        request_id=request_id,
                        outcome="internal_failure",
                        status_code=503,
                        provider_request_id=None,
                        started_at=started_at,
                        usage=attempt_start_usage,
                        cost_microusd=attempt_start_cost,
                        first_token_ms=None,
                        final_route=candidate,
                        price_id=(
                            candidate_price.price_id if candidate_price is not None else None
                        ),
                    )
                    if policy_engine is not None and policy_lease is not None:
                        await policy_engine.settle(
                            policy_lease,
                            attempt_start_usage.total_tokens,
                            attempt_start_cost,
                        )
                return _error(
                    request,
                    status_code=503,
                    code="INTERNAL_FAILURE",
                    message="Attempt accounting is unavailable",
                    retryable=True,
                )

        transport_result = await execute_provider_attempt(
            client,
            candidate,
            forwarded_headers=forwarded_request_headers,
            body=body,
            model=model,
        )
        if transport_result.failure == "provider_timeout":
            totals.ambiguous = True
            last_failure = "provider_timeout"
            await _settle_attempt_with_outbox(
                request.app.state.settlement_coordinator,
                recorder,
                metrics=metrics,
                route=candidate,
                request_id=request_id,
                attempt_id=attempt_id,
                outcome="timeout_ambiguous",
                status_code=None,
                provider_request_id=None,
                started_at=attempt_started_at,
                usage=UsageResult(),
                cost_microusd=None,
            )
            try:
                await _record_route_health(
                    route_health_engine,
                    candidate,
                    route_key=health_route_key,
                    token=health_token,
                    failed=True,
                )
            except RouteHealthUnavailableError:
                await logger.aexception("route_health_settlement_failed", route=health_route_key)
        elif transport_result.failure in ("connection_error", "transport_ambiguous"):
            if transport_result.failure == "transport_ambiguous":
                totals.ambiguous = True
            last_failure = "provider_unavailable"
            await _settle_attempt_with_outbox(
                request.app.state.settlement_coordinator,
                recorder,
                metrics=metrics,
                route=candidate,
                request_id=request_id,
                attempt_id=attempt_id,
                outcome=transport_result.failure,
                status_code=None,
                provider_request_id=None,
                started_at=attempt_started_at,
                usage=UsageResult(),
                cost_microusd=None,
            )
            try:
                await _record_route_health(
                    route_health_engine,
                    candidate,
                    route_key=health_route_key,
                    token=health_token,
                    failed=True,
                )
            except RouteHealthUnavailableError:
                await logger.aexception("route_health_settlement_failed", route=health_route_key)
        else:
            response = transport_result.response
            if response is None:
                raise RuntimeError("provider attempt completed without a response or failure")
            response_result = await resolve_retryable_response(
                response,
                candidate,
                has_next=has_next,
                started_at=attempt_started_at,
                price=candidate_price,
            )
            if response_result.response is None:
                if response_result.exhausted:
                    last_failure = "provider_unavailable"
                if response_result.outcome == "transport_ambiguous":
                    totals.ambiguous = True
                    await _settle_attempt_with_outbox(
                        request.app.state.settlement_coordinator,
                        recorder,
                        metrics=metrics,
                        route=candidate,
                        request_id=request_id,
                        attempt_id=attempt_id,
                        outcome=response_result.outcome,
                        status_code=response_result.status_code,
                        provider_request_id=response_result.provider_request_id,
                        started_at=attempt_started_at,
                        usage=response_result.usage,
                        cost_microusd=response_result.cost_microusd,
                    )
                    try:
                        await _record_route_health(
                            route_health_engine,
                            candidate,
                            route_key=health_route_key,
                            token=health_token,
                            failed=True,
                        )
                    except RouteHealthUnavailableError:
                        await logger.aexception(
                            "route_health_settlement_failed", route=health_route_key
                        )
                elif response_result.outcome == "retryable_status":
                    totals.add(response_result.usage, response_result.cost_microusd)
                    await _settle_attempt_with_outbox(
                        request.app.state.settlement_coordinator,
                        recorder,
                        metrics=metrics,
                        route=candidate,
                        request_id=request_id,
                        attempt_id=attempt_id,
                        outcome=response_result.outcome,
                        status_code=response_result.status_code,
                        provider_request_id=response_result.provider_request_id,
                        started_at=attempt_started_at,
                        usage=response_result.usage,
                        cost_microusd=response_result.cost_microusd,
                    )
                    try:
                        await _record_route_health(
                            route_health_engine,
                            candidate,
                            route_key=health_route_key,
                            token=health_token,
                            failed=(
                                response_result.status_code in candidate.health_failure_status_codes
                            ),
                        )
                    except RouteHealthUnavailableError:
                        await logger.aexception(
                            "route_health_settlement_failed", route=health_route_key
                        )
                else:
                    raise RuntimeError("consumed provider response has no terminal outcome")
            else:
                response = response_result.response
                return StreamingResponse(
                    _response_body(
                        response,
                        recorder=recorder,
                        request_id=request_id,
                        started_at=started_at,
                        policy_engine=policy_engine,
                        policy_lease=policy_lease,
                        price=candidate_price,
                        route=candidate,
                        attempt_id=attempt_id,
                        attempt_started_at=attempt_started_at,
                        totals=totals,
                        route_health_engine=route_health_engine,
                        route_health_key=health_route_key,
                        route_health_token=health_token,
                        exact_cache=exact_cache,
                        cache_key=cache_key,
                        cache_ttl_seconds=route.exact_cache_ttl_seconds,
                        cache_max_entry_bytes=settings.cache_max_entry_bytes,
                        metrics=metrics,
                        settlement_coordinator=request.app.state.settlement_coordinator,
                    ),
                    status_code=response.status_code,
                    headers=_downstream_headers(
                        response,
                        candidate,
                        policy_lease.headers if policy_lease is not None else {},
                        provider_attempt_count,
                        "MISS" if cache_key is not None else None,
                    ),
                )

        last_route = candidate
        last_price = candidate_price
        if has_next and settings.provider_retry_backoff_ms:
            backoff_ms = min(
                5000,
                settings.provider_retry_backoff_ms * (2 ** min(plan_index, 4)),
            )
            await asyncio.sleep(backoff_ms / 1000)

    status_code = (
        504
        if last_failure == "provider_timeout"
        else 503
        if last_failure == "routes_unhealthy"
        else 502
    )
    error_code = (
        "PROVIDER_TIMEOUT" if last_failure == "provider_timeout" else "PROVIDER_UNAVAILABLE"
    )
    final_usage = totals.usage()
    final_cost = None if totals.ambiguous else totals.cost_microusd if totals.has_cost else None
    durable_failure_settlement = False
    if recorder is not None:
        durable_failure_settlement = await _enqueue_request_settlement(
            request.app.state.settlement_coordinator,
            policy_engine=policy_engine,
            policy_lease=policy_lease,
            metrics=metrics,
            request_id=request_id,
            outcome=last_failure,
            status_code=status_code,
            provider_request_id=None,
            started_at=started_at,
            usage=final_usage,
            cost_microusd=final_cost,
            first_token_ms=None,
            final_route=last_route,
            price_id=last_price.price_id if last_price is not None else None,
        )
        if not durable_failure_settlement:
            await _settle_safely(
                recorder,
                metrics=metrics,
                request_id=request_id,
                outcome=last_failure,
                status_code=status_code,
                provider_request_id=None,
                started_at=started_at,
                usage=final_usage,
                cost_microusd=final_cost,
                first_token_ms=None,
                final_route=last_route,
                price_id=last_price.price_id if last_price is not None else None,
            )
    if not durable_failure_settlement and policy_engine is not None and policy_lease is not None:
        await policy_engine.settle(policy_lease, final_usage.total_tokens, final_cost)
    return _error(
        request,
        status_code=status_code,
        code=error_code,
        message="Provider timed out" if status_code == 504 else "Provider is unavailable",
        retryable=True,
        headers=policy_lease.headers if policy_lease is not None else None,
    )
