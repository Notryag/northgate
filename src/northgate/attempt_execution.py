from dataclasses import dataclass
from typing import Literal

import httpx

from northgate.provider_adapters import provider_adapter
from northgate.routing import ResolvedRoute

AttemptTransportFailure = Literal[
    "provider_timeout",
    "connection_error",
    "transport_ambiguous",
]


@dataclass(frozen=True)
class AttemptTransportResult:
    response: httpx.Response | None = None
    failure: AttemptTransportFailure | None = None


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
