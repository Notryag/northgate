from dataclasses import dataclass
from hashlib import sha256
from itertools import groupby
from uuid import UUID

from sqlalchemy import select

from northgate.config import Settings
from northgate.credentials import CredentialCipher
from northgate.db.database import Database
from northgate.db.models import ApplicationKey, Gateway, GatewayPolicy, ProviderCredential, Route


class InvalidApplicationKeyError(Exception):
    pass


class ForbiddenGatewayError(Exception):
    pass


class RouteUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class PolicyLimits:
    requests_per_minute: int | None = None
    concurrent_requests: int | None = None
    tokens_per_day: int | None = None
    daily_spend_microusd: int | None = None
    monthly_spend_microusd: int | None = None

    @property
    def enabled(self) -> bool:
        return any(
            limit is not None
            for limit in (
                self.requests_per_minute,
                self.concurrent_requests,
                self.tokens_per_day,
                self.daily_spend_microusd,
                self.monthly_spend_microusd,
            )
        )


@dataclass(frozen=True)
class ResolvedRoute:
    project_id: UUID | None
    gateway_id: UUID | None
    route_id: UUID | None
    provider: str
    base_url: str
    api_key: str
    allowed_metadata_keys: frozenset[str]
    policy: PolicyLimits
    priority: int = 0
    weight: int = 1
    match_metadata: tuple[tuple[str, str], ...] = ()
    exact_cache_ttl_seconds: int | None = None
    max_retries: int = 0
    retry_status_codes: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    health_failure_threshold: int = 0
    health_recovery_seconds: int = 30
    health_failure_status_codes: frozenset[int] = frozenset({500, 502, 503, 504})


class DatabaseRouteResolver:
    def __init__(self, database: Database, cipher: CredentialCipher) -> None:
        self.database = database
        self.cipher = cipher

    async def resolve(self, application_key: str, gateway_slug: str) -> list[ResolvedRoute]:
        digest = sha256(application_key.encode()).hexdigest()
        async with self.database.sessions() as session:
            application = await session.scalar(
                select(ApplicationKey).where(
                    ApplicationKey.key_digest == digest,
                    ApplicationKey.revoked_at.is_(None),
                )
            )
            if application is None:
                raise InvalidApplicationKeyError

            gateway = await session.scalar(
                select(Gateway).where(
                    Gateway.project_id == application.project_id,
                    Gateway.slug == gateway_slug,
                )
            )
            if gateway is None:
                raise ForbiddenGatewayError

            result = await session.execute(
                select(Route, ProviderCredential)
                .join(
                    ProviderCredential,
                    ProviderCredential.id == Route.provider_credential_id,
                )
                .where(Route.gateway_id == gateway.id, Route.enabled.is_(True))
                .order_by(Route.priority, Route.id)
            )
            rows = result.all()
            if not rows:
                raise RouteUnavailableError
            policy = await session.scalar(
                select(GatewayPolicy).where(GatewayPolicy.gateway_id == gateway.id)
            )

        resolved: list[ResolvedRoute] = []
        for route, credential in rows:
            try:
                api_key = self.cipher.decrypt(credential.encrypted_api_key)
            except ValueError as exc:
                raise RouteUnavailableError from exc
            resolved.append(
                ResolvedRoute(
                    project_id=application.project_id,
                    gateway_id=gateway.id,
                    route_id=route.id,
                    provider=credential.provider,
                    base_url=credential.base_url,
                    api_key=api_key,
                    allowed_metadata_keys=frozenset(application.allowed_metadata_keys),
                    policy=PolicyLimits(
                        requests_per_minute=policy.requests_per_minute if policy else None,
                        concurrent_requests=policy.concurrent_requests if policy else None,
                        tokens_per_day=policy.tokens_per_day if policy else None,
                        daily_spend_microusd=policy.daily_spend_microusd if policy else None,
                        monthly_spend_microusd=policy.monthly_spend_microusd if policy else None,
                    ),
                    priority=route.priority,
                    weight=route.weight,
                    match_metadata=tuple(sorted(route.match_metadata.items())),
                    exact_cache_ttl_seconds=(policy.exact_cache_ttl_seconds if policy else None),
                    max_retries=route.max_retries,
                    retry_status_codes=frozenset(route.retry_status_codes),
                    health_failure_threshold=route.health_failure_threshold,
                    health_recovery_seconds=route.health_recovery_seconds,
                    health_failure_status_codes=frozenset(route.health_failure_status_codes),
                )
            )
        return resolved


def configured_route(settings: Settings) -> ResolvedRoute:
    provider_key = settings.provider_api_key
    if provider_key is None or not provider_key.get_secret_value():
        raise RouteUnavailableError
    return ResolvedRoute(
        project_id=None,
        gateway_id=None,
        route_id=None,
        provider="openai",
        base_url=settings.provider_base_url,
        api_key=provider_key.get_secret_value(),
        allowed_metadata_keys=frozenset(
            key.strip() for key in settings.allowed_metadata_keys.split(",") if key.strip()
        ),
        policy=PolicyLimits(
            requests_per_minute=settings.request_limit_per_minute,
            concurrent_requests=settings.concurrency_limit,
            tokens_per_day=settings.token_limit_per_day,
            daily_spend_microusd=settings.daily_spend_limit_microusd,
            monthly_spend_microusd=settings.monthly_spend_limit_microusd,
        ),
        priority=0,
        exact_cache_ttl_seconds=settings.exact_cache_ttl_seconds,
        max_retries=settings.provider_max_retries,
        retry_status_codes=frozenset(
            int(code.strip())
            for code in settings.provider_retry_status_codes.split(",")
            if code.strip()
        ),
        health_failure_threshold=(
            settings.route_health_failure_threshold if settings.route_health_enabled else 0
        ),
        health_recovery_seconds=settings.route_health_recovery_seconds,
        health_failure_status_codes=frozenset(
            int(code.strip())
            for code in settings.route_health_failure_status_codes.split(",")
            if code.strip()
        ),
    )


def configured_routes(settings: Settings) -> list[ResolvedRoute]:
    primary = configured_route(settings)
    fallback_key = settings.fallback_provider_api_key
    if (
        not settings.fallback_provider_name
        or not settings.fallback_provider_base_url
        or fallback_key is None
        or not fallback_key.get_secret_value()
    ):
        return [primary]
    return [
        primary,
        ResolvedRoute(
            project_id=primary.project_id,
            gateway_id=primary.gateway_id,
            route_id=None,
            provider=settings.fallback_provider_name,
            base_url=settings.fallback_provider_base_url,
            api_key=fallback_key.get_secret_value(),
            allowed_metadata_keys=primary.allowed_metadata_keys,
            policy=primary.policy,
            priority=1,
            exact_cache_ttl_seconds=primary.exact_cache_ttl_seconds,
            max_retries=settings.fallback_provider_max_retries,
            retry_status_codes=primary.retry_status_codes,
            health_failure_threshold=primary.health_failure_threshold,
            health_recovery_seconds=primary.health_recovery_seconds,
            health_failure_status_codes=primary.health_failure_status_codes,
        ),
    ]


def select_routes(
    routes: list[ResolvedRoute],
    metadata: dict[str, str],
    selection_key: str,
) -> list[ResolvedRoute]:
    matching = [
        route
        for route in routes
        if all(metadata.get(key) == value for key, value in route.match_metadata)
    ]
    matching.sort(key=lambda route: (route.priority, -len(route.match_metadata)))

    ordered: list[ResolvedRoute] = []
    for tier, grouped in groupby(
        matching,
        key=lambda route: (route.priority, len(route.match_metadata)),
    ):
        remaining = list(grouped)
        round_index = 0
        while remaining:
            total_weight = sum(route.weight for route in remaining)
            digest = sha256(f"{selection_key}\0{tier}\0{round_index}".encode()).digest()
            ticket = int.from_bytes(digest[:8], "big") % total_weight
            cumulative = 0
            for index, route in enumerate(remaining):
                cumulative += route.weight
                if ticket < cumulative:
                    ordered.append(remaining.pop(index))
                    break
            round_index += 1
    return ordered
