import asyncio
import time
from datetime import UTC, datetime
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
from northgate.pricing import PricingRepository, configured_price
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
    request_metadata as parse_request_metadata,
)
from northgate.proxy_settlement import (
    AttemptTotals,
)
from northgate.proxy_settlement import (
    enqueue_request_settlement as _enqueue_request_settlement,
)
from northgate.proxy_settlement import (
    settle_attempt_with_outbox as _settle_attempt_with_outbox,
)
from northgate.proxy_settlement import (
    settle_request_safely as _settle_safely,
)
from northgate.route_health import (
    RouteHealthEngine,
    RouteHealthUnavailableError,
)
from northgate.route_health import (
    record_route_health as _record_route_health,
)
from northgate.route_health import (
    route_health_key as _route_health_key,
)
from northgate.route_planning import (
    accounting_metadata,
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
from northgate.stream_finalization import stream_response_body as _response_body
from northgate.token_reservation import estimate_token_reservation
from northgate.tracing import Tracing, add_span_event
from northgate.usage import (
    DuplicateRequestError,
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


def _route_label(route: ResolvedRoute) -> str:
    return str(route.route_id) if route.route_id is not None else f"configured-{route.provider}"


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
    routes = plan_routes(routes, request_id, request_metadata)
    if not routes:
        return _error(
            request,
            status_code=503,
            code="ROUTE_NOT_MATCHED",
            message="No route matches the request metadata",
            retryable=False,
        )
    route = routes[0]
    ledger_metadata, ledger_metadata_trust = accounting_metadata(route, request_metadata)

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
                    request_metadata=ledger_metadata,
                    request_metadata_trust=ledger_metadata_trust,
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

    possible_attempts = sum(candidate.max_retries + 1 for candidate in routes)
    reservation = estimate_token_reservation(
        body,
        model=model,
        route_default_output_tokens=route.default_max_output_tokens,
        model_output_defaults=settings.policy_model_max_output_tokens,
        global_default_output_tokens=settings.policy_default_max_output_tokens,
        margin_percent=settings.policy_prompt_margin_percent,
        attempt_multiplier=possible_attempts,
    )
    if metrics is not None:
        metrics.observe_token_reservation(reservation)
    margin_per_attempt = reservation.reservation_margin_tokens // possible_attempts
    estimated_cost_microusd = sum(
        (
            price.cost(
                reservation.estimated_prompt_tokens + margin_per_attempt,
                reservation.reserved_output_tokens,
            )
            if price is not None
            else 0
        )
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
            policy_lease = await policy_engine.admit(
                gateway_key=str(route.gateway_id or gateway_slug),
                request_id=request_id,
                limits=route.policy,
                estimated_tokens=reservation.reserved_total_tokens,
                estimated_cost_microusd=estimated_cost_microusd,
            )
        except PolicyRejectedError as exc:
            if recorder is not None:
                try:
                    await recorder.record_rejection(
                        request_id=request_id,
                        route=route,
                        model=model,
                        request_metadata=ledger_metadata,
                        request_metadata_trust=ledger_metadata_trust,
                        price_id=prices[0].price_id if prices[0] is not None else None,
                        estimated_tokens=reservation.reserved_total_tokens,
                        estimated_prompt_tokens=reservation.estimated_prompt_tokens,
                        reserved_output_tokens=reservation.reserved_output_tokens,
                        attempt_multiplier=reservation.attempt_multiplier,
                        reservation_margin_tokens=reservation.reservation_margin_tokens,
                        token_estimator=reservation.estimator,
                        output_limit_source=reservation.output_limit_source,
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
                request_metadata=ledger_metadata,
                request_metadata_trust=ledger_metadata_trust,
                price_id=price.price_id if price is not None else None,
                estimated_tokens=reservation.reserved_total_tokens,
                estimated_prompt_tokens=reservation.estimated_prompt_tokens,
                reserved_output_tokens=reservation.reserved_output_tokens,
                attempt_multiplier=reservation.attempt_multiplier,
                reservation_margin_tokens=reservation.reservation_margin_tokens,
                token_estimator=reservation.estimator,
                output_limit_source=reservation.output_limit_source,
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
                        reserved_tokens=reservation.reserved_total_tokens,
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
                    reserved_tokens=reservation.reserved_total_tokens,
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
                        reserved_tokens=reservation.reserved_total_tokens,
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
            reserved_tokens=reservation.reserved_total_tokens,
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
