from dataclasses import dataclass
from hashlib import sha256
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

    @property
    def enabled(self) -> bool:
        return any(
            limit is not None
            for limit in (
                self.requests_per_minute,
                self.concurrent_requests,
                self.tokens_per_day,
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


class DatabaseRouteResolver:
    def __init__(self, database: Database, cipher: CredentialCipher) -> None:
        self.database = database
        self.cipher = cipher

    async def resolve(self, application_key: str, gateway_slug: str) -> ResolvedRoute:
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
                .order_by(Route.priority)
                .limit(1)
            )
            row = result.one_or_none()
            if row is None:
                raise RouteUnavailableError
            route, credential = row
            policy = await session.scalar(
                select(GatewayPolicy).where(GatewayPolicy.gateway_id == gateway.id)
            )

        try:
            api_key = self.cipher.decrypt(credential.encrypted_api_key)
        except ValueError as exc:
            raise RouteUnavailableError from exc
        return ResolvedRoute(
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
            ),
        )


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
        ),
    )
