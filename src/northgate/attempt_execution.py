from dataclasses import dataclass, field
from typing import Literal

import httpx

from northgate.pricing import PriceQuote
from northgate.provider_adapters import provider_adapter
from northgate.routing import ResolvedRoute
from northgate.usage import UsageAccumulator, UsageResult

AttemptTransportFailure = Literal[
    "provider_timeout",
    "connection_error",
    "transport_ambiguous",
]


@dataclass(frozen=True)
class AttemptTransportResult:
    response: httpx.Response | None = None
    failure: AttemptTransportFailure | None = None


@dataclass(frozen=True)
class RetryableResponseResult:
    response: httpx.Response | None = None
    outcome: Literal["retryable_status", "transport_ambiguous"] | None = None
    status_code: int | None = None
    provider_request_id: str | None = None
    usage: UsageResult = field(default_factory=UsageResult)
    cost_microusd: int | None = None
    exhausted: bool = False


async def execute_provider_attempt(
    client: httpx.AsyncClient,
    route: ResolvedRoute,
    *,
    forwarded_headers: dict[str, str],
    body: bytes,
    model: str | None,
) -> AttemptTransportResult:
    upstream_request = provider_adapter(route.adapter).build_request(
        client,
        route,
        forwarded_headers=forwarded_headers,
        body=body,
        model=model,
    )
    try:
        response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException:
        return AttemptTransportResult(failure="provider_timeout")
    except httpx.ConnectError:
        return AttemptTransportResult(failure="connection_error")
    except httpx.TransportError:
        return AttemptTransportResult(failure="transport_ambiguous")
    return AttemptTransportResult(response=response)


async def resolve_retryable_response(
    response: httpx.Response,
    route: ResolvedRoute,
    *,
    has_next: bool,
    started_at: float,
    price: PriceQuote | None,
) -> RetryableResponseResult:
    retryable = response.status_code in route.retry_status_codes
    exhausted_server_error = retryable and not has_next and response.status_code >= 500
    if not retryable or (not has_next and not exhausted_server_error):
        return RetryableResponseResult(response=response)

    status_code = response.status_code
    provider_request_id = response.headers.get("x-request-id")
    accumulator = UsageAccumulator(response.headers.get("content-type", ""), started_at)
    try:
        try:
            if response.is_stream_consumed:
                accumulator.observe(response.content)
            else:
                async for chunk in response.aiter_raw():
                    accumulator.observe(chunk)
        finally:
            await response.aclose()
    except httpx.TransportError:
        return RetryableResponseResult(
            outcome="transport_ambiguous",
            status_code=status_code,
            provider_request_id=provider_request_id,
            exhausted=exhausted_server_error,
        )

    usage = accumulator.result()
    cost_microusd = (
        price.usage_cost(usage.prompt_tokens, usage.completion_tokens)
        if price is not None
        else None
    )
    return RetryableResponseResult(
        outcome="retryable_status",
        status_code=status_code,
        provider_request_id=provider_request_id,
        usage=usage,
        cost_microusd=cost_microusd,
        exhausted=exhausted_server_error,
    )
