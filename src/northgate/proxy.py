import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest

import httpx
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from northgate.config import Settings
from northgate.policy import (
    PolicyEngine,
    PolicyLease,
    PolicyRejectedError,
    PolicyUnavailableError,
)
from northgate.pricing import PriceQuote, PricingRepository, configured_price
from northgate.routing import (
    DatabaseRouteResolver,
    ForbiddenGatewayError,
    InvalidApplicationKeyError,
    ResolvedRoute,
    RouteUnavailableError,
    configured_route,
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


def _upstream_headers(request: Request, provider_api_key: str) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }
    headers["authorization"] = f"Bearer {provider_api_key}"
    return headers


def _downstream_headers(
    response: httpx.Response,
    route: ResolvedRoute,
    policy_headers: dict[str, str],
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() in _FORWARDED_RESPONSE_HEADERS
        or name.lower().startswith(("openai-", "x-ratelimit-"))
    }
    headers["Northgate-Provider"] = route.provider
    headers["Northgate-Route"] = str(route.route_id or "configured-openai")
    headers.update(policy_headers)
    return headers


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
        )
    except Exception:
        await logger.aexception("usage_settlement_failed", northgate_request_id=request_id)


async def _response_body(
    response: httpx.Response,
    *,
    recorder: UsageRecorder | None,
    request_id: str,
    started_at: float,
    policy_engine: PolicyEngine | None,
    policy_lease: PolicyLease | None,
    price: PriceQuote | None,
) -> AsyncIterator[bytes]:
    accumulator = UsageAccumulator(response.headers.get("content-type", ""), started_at)
    outcome = "succeeded" if response.status_code < 400 else "provider_error"
    try:
        if response.is_stream_consumed:
            accumulator.observe(response.content)
            yield response.content
            return
        async for chunk in response.aiter_raw():
            accumulator.observe(chunk)
            yield chunk
    except asyncio.CancelledError:
        outcome = "client_disconnected"
        raise
    except httpx.TransportError:
        outcome = "provider_error"
        raise
    finally:
        await response.aclose()
        usage = accumulator.result()
        cost_microusd = (
            price.usage_cost(usage.prompt_tokens, usage.completion_tokens)
            if price is not None
            else None
        )
        if recorder is not None:
            settlement = asyncio.create_task(
                _settle_safely(
                    recorder,
                    request_id=request_id,
                    outcome=outcome,
                    status_code=response.status_code,
                    provider_request_id=response.headers.get("x-request-id"),
                    started_at=started_at,
                    usage=usage,
                    cost_microusd=cost_microusd,
                    first_token_ms=accumulator.first_token_ms,
                )
            )
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                pass
        if policy_engine is not None and policy_lease is not None:
            settlement = asyncio.create_task(
                policy_engine.settle(policy_lease, usage.total_tokens, cost_microusd)
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


async def _resolve_route(
    request: Request,
    gateway_slug: str,
    settings: Settings,
) -> ResolvedRoute:
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
    return configured_route(settings)


async def proxy_chat_completions(
    request: Request,
    gateway_slug: str,
) -> Response:
    settings: Settings = request.app.state.settings
    started_at = time.perf_counter()
    request_id: str = request.state.request_id

    try:
        route = await _resolve_route(request, gateway_slug, settings)
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

    request_metadata = _request_metadata(request, route)
    if request_metadata is None:
        return _error(
            request,
            status_code=400,
            code="INVALID_METADATA",
            message="Invalid request metadata",
            retryable=False,
        )

    body = await request.body()
    model = _request_model(body)
    pricing_repository: PricingRepository | None = request.app.state.pricing_repository
    if settings.routing_source == "database" and pricing_repository is not None and model:
        price = await pricing_repository.resolve(route.provider, model, datetime.now(UTC))
    else:
        price = configured_price(settings)
    spend_limited = (
        route.policy.daily_spend_microusd is not None
        or route.policy.monthly_spend_microusd is not None
    )
    if spend_limited and price is None:
        return _error(
            request,
            status_code=503,
            code="PRICING_UNAVAILABLE",
            message="Model pricing is unavailable",
            retryable=False,
        )

    estimated_input_tokens, estimated_output_tokens = _estimated_tokens(body, settings)
    estimated_cost_microusd = (
        price.cost(estimated_input_tokens, estimated_output_tokens) if price is not None else 0
    )
    policy_engine: PolicyEngine | None = request.app.state.policy_engine
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
                estimated_tokens=estimated_input_tokens + estimated_output_tokens,
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

    recorder: UsageRecorder | None = request.app.state.usage_recorder
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

    client: httpx.AsyncClient = request.app.state.upstream_client
    upstream_url = f"{route.base_url.rstrip('/')}/chat/completions"
    upstream_request = client.build_request(
        "POST",
        upstream_url,
        headers=_upstream_headers(request, route.api_key),
        content=body,
    )

    try:
        response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException:
        if recorder is not None:
            await _settle_safely(
                recorder,
                request_id=request_id,
                outcome="provider_timeout",
                status_code=504,
                provider_request_id=None,
                started_at=started_at,
                usage=UsageResult(),
                cost_microusd=None,
                first_token_ms=None,
            )
        if policy_engine is not None and policy_lease is not None:
            await policy_engine.settle(policy_lease, None)
        return _error(
            request,
            status_code=504,
            code="PROVIDER_TIMEOUT",
            message="Provider timed out",
            retryable=True,
        )
    except httpx.TransportError:
        if recorder is not None:
            await _settle_safely(
                recorder,
                request_id=request_id,
                outcome="provider_unavailable",
                status_code=502,
                provider_request_id=None,
                started_at=started_at,
                usage=UsageResult(),
                cost_microusd=None,
                first_token_ms=None,
            )
        if policy_engine is not None and policy_lease is not None:
            await policy_engine.settle(policy_lease, None)
        return _error(
            request,
            status_code=502,
            code="PROVIDER_UNAVAILABLE",
            message="Provider is unavailable",
            retryable=True,
        )

    return StreamingResponse(
        _response_body(
            response,
            recorder=recorder,
            request_id=request_id,
            started_at=started_at,
            policy_engine=policy_engine,
            policy_lease=policy_lease,
            price=price,
        ),
        status_code=response.status_code,
        headers=_downstream_headers(
            response,
            route,
            policy_lease.headers if policy_lease is not None else {},
        ),
    )
