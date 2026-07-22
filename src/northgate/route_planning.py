from hashlib import sha256
from hmac import compare_digest

from fastapi import Request

from northgate.config import Settings
from northgate.provider_adapters import provider_adapter
from northgate.routing import (
    DatabaseRouteResolver,
    ForbiddenGatewayError,
    InvalidApplicationKeyError,
    ResolvedRoute,
    configured_routes,
    select_routes,
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


async def resolve_routes(
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


def plan_routes(
    routes: list[ResolvedRoute],
    request_id: str,
    caller_metadata: dict[str, str],
) -> list[ResolvedRoute]:
    if not routes:
        return []
    metadata = (
        caller_metadata
        if routes[0].metadata_routing_mode == "legacy"
        else dict(routes[0].trusted_metadata)
    )
    return select_routes(routes, metadata, request_id)


def accounting_metadata(
    route: ResolvedRoute,
    caller_metadata: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    metadata = dict(caller_metadata)
    caller_trust = "legacy" if route.metadata_routing_mode == "legacy" else "untrusted"
    trust = {key: caller_trust for key in caller_metadata}
    for key, value in route.trusted_metadata:
        metadata[key] = value
        trust[key] = "server" if key.startswith("northgate.") else "fixed"
    return metadata, trust


def validate_primary_route(route: ResolvedRoute, model: str | None) -> None:
    provider_adapter(route.adapter).validate(route, model)
