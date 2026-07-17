import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

import httpx
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from northgate.config import Settings
from northgate.exact_cache import (
    CacheEntry,
    CacheUnavailableError,
    ExactCache,
    exact_cache_key,
)
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
from northgate.route_health import RouteHealthEngine, RouteHealthUnavailableError
from northgate.routing import (
    DatabaseRouteResolver,
    ForbiddenGatewayError,
    InvalidApplicationKeyError,
    ResolvedRoute,
    RouteUnavailableError,
    configured_routes,
    select_routes,
)
from northgate.usage import (
    DuplicateRequestError,
    UsageAccumulator,
    UsageRecorder,
    UsageResult,
)

logger = structlog.get_logger()

_MAX_METADATA_BYTES = 8 * 1024
_MAX_METADATA_KEYS = 32
_MAX_METADATA_KEY_LENGTH = 64
_MAX_METADATA_VALUE_LENGTH = 256
_FORWARDED_REQUEST_HEADERS = {"accept", "content-type"}
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


def _bearer_credential(request: Request) -> str | None:
    scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not credential:
        return None
    return credential


def _configured_application_key_is_valid(credential: str, settings: Settings) -> bool:
    expected = settings.application_key_sha256
    if expected is None or not expected.get_secret_value():
        return False
    actual = sha256(credential.encode()).hexdigest()
    return compare_digest(actual, expected.get_secret_value())


def _forwarded_request_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }


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
        await logger.aexception("usage_settlement_failed", northgate_request_id=request_id)


@dataclass
class AttemptTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: int = 0
    has_usage: bool = False
    has_cost: bool = False
    ambiguous: bool = False

    def add(self, usage: UsageResult, cost_microusd: int | None) -> None:
        if usage.total_tokens is not None:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0
            self.total_tokens += usage.total_tokens
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
        )


async def _settle_attempt_safely(
    recorder: UsageRecorder | None,
    *,
    attempt_id: UUID | None,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
) -> None:
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
        await logger.aexception("provider_attempt_settlement_failed", attempt_id=str(attempt_id))


async def _consume_retryable_response(
    response: httpx.Response,
    *,
    started_at: float,
    price: PriceQuote | None,
) -> tuple[UsageResult, int | None]:
    accumulator = UsageAccumulator(response.headers.get("content-type", ""), started_at)
    try:
        if response.is_stream_consumed:
            accumulator.observe(response.content)
        else:
            async for chunk in response.aiter_raw():
                accumulator.observe(chunk)
    finally:
        await response.aclose()
    usage = accumulator.result()
    cost_microusd = (
        price.usage_cost(usage.prompt_tokens, usage.completion_tokens)
        if price is not None
        else None
    )
    return usage, cost_microusd


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


async def _response_body(
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
) -> AsyncIterator[bytes]:
    accumulator = UsageAccumulator(response.headers.get("content-type", ""), started_at)
    outcome = "succeeded" if response.status_code < 400 else "provider_error"
    transport_failed = False
    completed = False
    cache_body: bytearray | None = bytearray() if cache_key is not None else None
    try:
        if response.is_stream_consumed:
            accumulator.observe(response.content)
            if cache_body is not None:
                if len(response.content) <= cache_max_entry_bytes:
                    cache_body.extend(response.content)
                else:
                    cache_body = None
            yield response.content
            completed = True
            return
        async for chunk in response.aiter_raw():
            accumulator.observe(chunk)
            if cache_body is not None:
                if len(cache_body) + len(chunk) <= cache_max_entry_bytes:
                    cache_body.extend(chunk)
                else:
                    cache_body = None
            yield chunk
        completed = True
    except asyncio.CancelledError:
        outcome = "client_disconnected"
        totals.ambiguous = True
        raise
    except httpx.TransportError:
        outcome = "provider_error"
        transport_failed = True
        totals.ambiguous = True
        raise
    finally:
        await response.aclose()
        if (
            completed
            and 200 <= response.status_code < 300
            and cache_body is not None
            and exact_cache is not None
            and cache_key is not None
            and cache_ttl_seconds is not None
        ):
            try:
                await exact_cache.set(
                    cache_key,
                    CacheEntry(
                        status_code=response.status_code,
                        headers=_cached_response_headers(response),
                        body=bytes(cache_body),
                        route_key=route_health_key,
                    ),
                    cache_ttl_seconds,
                )
            except CacheUnavailableError:
                await logger.awarning(
                    "exact_cache_write_failed",
                    northgate_request_id=request_id,
                )
        if outcome != "client_disconnected":
            health_failed = transport_failed or (
                response.status_code in route.health_failure_status_codes
            )
            try:
                await _record_route_health(
                    route_health_engine,
                    route,
                    route_key=route_health_key,
                    token=route_health_token,
                    failed=health_failed,
                )
            except RouteHealthUnavailableError:
                await logger.aexception(
                    "route_health_settlement_failed",
                    route=route_health_key,
                    northgate_request_id=request_id,
                )
        usage = accumulator.result()
        cost_microusd = (
            price.usage_cost(usage.prompt_tokens, usage.completion_tokens)
            if price is not None
            else None
        )
        totals.add(usage, cost_microusd)
        await _settle_attempt_safely(
            recorder,
            attempt_id=attempt_id,
            outcome=outcome,
            status_code=response.status_code,
            provider_request_id=response.headers.get("x-request-id"),
            started_at=attempt_started_at,
            usage=usage,
            cost_microusd=cost_microusd,
        )
        aggregate_usage = totals.usage()
        aggregate_cost = totals.cost_microusd if totals.has_cost else None
        if recorder is not None:
            settlement = asyncio.create_task(
                _settle_safely(
                    recorder,
                    request_id=request_id,
                    outcome=outcome,
                    status_code=response.status_code,
                    provider_request_id=response.headers.get("x-request-id"),
                    started_at=started_at,
                    usage=aggregate_usage,
                    cost_microusd=aggregate_cost,
                    first_token_ms=accumulator.first_token_ms,
                    final_route=route,
                    price_id=price.price_id if price is not None else None,
                )
            )
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                pass
        if policy_engine is not None and policy_lease is not None:
            settlement = asyncio.create_task(
                policy_engine.settle(
                    policy_lease,
                    None if totals.ambiguous else aggregate_usage.total_tokens,
                    None if totals.ambiguous else aggregate_cost,
                )
            )
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                pass


def _request_model(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


def _request_metadata(request: Request, route: ResolvedRoute) -> dict[str, str] | None:
    encoded = request.headers.get("northgate-metadata")
    if encoded is None:
        return {}
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        return None
    try:
        metadata = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict) or len(metadata) > _MAX_METADATA_KEYS:
        return None
    for key, value in metadata.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or len(key) > _MAX_METADATA_KEY_LENGTH
            or len(value) > _MAX_METADATA_VALUE_LENGTH
            or key.startswith("northgate.")
            or key not in route.allowed_metadata_keys
        ):
            return None
    return metadata


def _estimated_tokens(body: bytes, settings: Settings) -> tuple[int, int]:
    prompt_estimate = math.ceil(len(body) / 3)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return prompt_estimate, settings.policy_default_max_output_tokens
    configured_max = payload.get("max_completion_tokens", payload.get("max_tokens"))
    output_estimate = (
        configured_max
        if isinstance(configured_max, int)
        and not isinstance(configured_max, bool)
        and configured_max > 0
        else settings.policy_default_max_output_tokens
    )
    return prompt_estimate, output_estimate


async def _resolve_routes(
    request: Request,
    gateway_slug: str,
    settings: Settings,
) -> list[ResolvedRoute]:
    credential = _bearer_credential(request)
    if credential is None:
        raise InvalidApplicationKeyError

    if settings.routing_source == "database":
        resolver: DatabaseRouteResolver = request.app.state.route_resolver
        return await resolver.resolve(credential, gateway_slug)

    if not _configured_application_key_is_valid(credential, settings):
        raise InvalidApplicationKeyError
    if gateway_slug != settings.gateway_slug:
        raise ForbiddenGatewayError
    return configured_routes(settings)


async def proxy_chat_completions(
    request: Request,
    gateway_slug: str,
) -> Response:
    settings: Settings = request.app.state.settings
    started_at = time.perf_counter()
    request_id: str = request.state.request_id

    try:
        routes = await _resolve_routes(request, gateway_slug, settings)
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
    request_metadata = _request_metadata(request, route)
    if request_metadata is None:
        return _error(
            request,
            status_code=400,
            code="INVALID_METADATA",
            message="Invalid request metadata",
            retryable=False,
        )
    routes = select_routes(routes, request_metadata, request_id)
    if not routes:
        return _error(
            request,
            status_code=503,
            code="ROUTE_NOT_MATCHED",
            message="No route matches the request metadata",
            retryable=False,
        )
    route = routes[0]

    body = await request.body()
    model = _request_model(body)
    try:
        for candidate in routes:
            provider_adapter(candidate.adapter).validate(candidate, model)
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

    forwarded_request_headers = _forwarded_request_headers(request)
    policy_engine: PolicyEngine | None = request.app.state.policy_engine
    recorder: UsageRecorder | None = request.app.state.usage_recorder
    exact_cache: ExactCache | None = request.app.state.exact_cache
    cache_key: str | None = None
    cache_entry: CacheEntry | None = None
    if route.exact_cache_ttl_seconds is not None and exact_cache is not None:
        cache_key = exact_cache_key(
            gateway_key=str(route.gateway_id or gateway_slug),
            body=body,
            metadata=request_metadata,
            request_headers=forwarded_request_headers,
            routes=routes,
        )
        try:
            cache_entry = await exact_cache.get(cache_key)
        except CacheUnavailableError:
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
            await _settle_safely(
                recorder,
                request_id=request_id,
                outcome="cache_hit",
                status_code=cache_entry.status_code,
                provider_request_id=None,
                started_at=started_at,
                usage=UsageResult(prompt_tokens=0, completion_tokens=0, total_tokens=0),
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

    estimated_input_tokens, estimated_output_tokens = _estimated_tokens(body, settings)
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
            policy_lease = await policy_engine.admit(
                gateway_key=str(route.gateway_id or gateway_slug),
                request_id=request_id,
                limits=route.policy,
                estimated_tokens=(estimated_input_tokens + estimated_output_tokens)
                * possible_attempts,
                estimated_cost_microusd=estimated_cost_microusd,
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

    price = prices[0]
    if recorder is not None:
        try:
            await recorder.start(
                request_id=request_id,
                route=route,
                model=model,
                request_metadata=request_metadata,
                price_id=price.price_id if price is not None else None,
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

    for plan_index, (candidate, candidate_price) in enumerate(attempt_plan):
        has_next = plan_index + 1 < len(attempt_plan)
        health_route_key = _route_health_key(candidate)
        health_token = f"{request_id}:{plan_index + 1}"
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
                if recorder is not None:
                    await _settle_safely(
                        recorder,
                        request_id=request_id,
                        outcome="route_health_unavailable",
                        status_code=503,
                        provider_request_id=None,
                        started_at=started_at,
                        usage=totals.usage(),
                        cost_microusd=totals.cost_microusd if totals.has_cost else None,
                        first_token_ms=None,
                        final_route=candidate,
                        price_id=(
                            candidate_price.price_id if candidate_price is not None else None
                        ),
                    )
                if policy_engine is not None and policy_lease is not None:
                    await policy_engine.settle(
                        policy_lease,
                        None if totals.ambiguous else totals.usage().total_tokens,
                        None
                        if totals.ambiguous
                        else totals.cost_microusd
                        if totals.has_cost
                        else None,
                    )
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
                await _settle_safely(
                    recorder,
                    request_id=request_id,
                    outcome="internal_failure",
                    status_code=503,
                    provider_request_id=None,
                    started_at=started_at,
                    usage=totals.usage(),
                    cost_microusd=totals.cost_microusd if totals.has_cost else None,
                    first_token_ms=None,
                    final_route=candidate,
                    price_id=candidate_price.price_id if candidate_price is not None else None,
                )
                if policy_engine is not None and policy_lease is not None:
                    await policy_engine.settle(policy_lease, 0, 0)
                return _error(
                    request,
                    status_code=503,
                    code="INTERNAL_FAILURE",
                    message="Attempt accounting is unavailable",
                    retryable=True,
                )

        upstream_request = provider_adapter(candidate.adapter).build_request(
            client,
            candidate,
            forwarded_headers=forwarded_request_headers,
            body=body,
            model=model,
        )
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            totals.ambiguous = True
            last_failure = "provider_timeout"
            await _settle_attempt_safely(
                recorder,
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
        except httpx.TransportError as exc:
            is_connection_failure = isinstance(exc, httpx.ConnectError)
            if not is_connection_failure:
                totals.ambiguous = True
            last_failure = "provider_unavailable"
            await _settle_attempt_safely(
                recorder,
                attempt_id=attempt_id,
                outcome="connection_error" if is_connection_failure else "transport_ambiguous",
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
            if response.status_code in candidate.retry_status_codes and has_next:
                try:
                    retry_usage, retry_cost = await _consume_retryable_response(
                        response,
                        started_at=attempt_started_at,
                        price=candidate_price,
                    )
                except httpx.TransportError:
                    totals.ambiguous = True
                    await _settle_attempt_safely(
                        recorder,
                        attempt_id=attempt_id,
                        outcome="transport_ambiguous",
                        status_code=response.status_code,
                        provider_request_id=response.headers.get("x-request-id"),
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
                        await logger.aexception(
                            "route_health_settlement_failed", route=health_route_key
                        )
                else:
                    totals.add(retry_usage, retry_cost)
                    await _settle_attempt_safely(
                        recorder,
                        attempt_id=attempt_id,
                        outcome="retryable_status",
                        status_code=response.status_code,
                        provider_request_id=response.headers.get("x-request-id"),
                        started_at=attempt_started_at,
                        usage=retry_usage,
                        cost_microusd=retry_cost,
                    )
                    try:
                        await _record_route_health(
                            route_health_engine,
                            candidate,
                            route_key=health_route_key,
                            token=health_token,
                            failed=(response.status_code in candidate.health_failure_status_codes),
                        )
                    except RouteHealthUnavailableError:
                        await logger.aexception(
                            "route_health_settlement_failed", route=health_route_key
                        )
            else:
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
    if recorder is not None:
        await _settle_safely(
            recorder,
            request_id=request_id,
            outcome=last_failure,
            status_code=status_code,
            provider_request_id=None,
            started_at=started_at,
            usage=totals.usage(),
            cost_microusd=totals.cost_microusd if totals.has_cost else None,
            first_token_ms=None,
            final_route=last_route,
            price_id=last_price.price_id if last_price is not None else None,
        )
    if policy_engine is not None and policy_lease is not None:
        await policy_engine.settle(
            policy_lease,
            None if totals.ambiguous else totals.usage().total_tokens,
            None if totals.ambiguous else totals.cost_microusd if totals.has_cost else None,
        )
    return _error(
        request,
        status_code=status_code,
        code=error_code,
        message="Provider timed out" if status_code == 504 else "Provider is unavailable",
        retryable=True,
        headers=policy_lease.headers if policy_lease is not None else None,
    )
