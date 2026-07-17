import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from northgate.config import Settings, get_settings
from northgate.credentials import CredentialCipher
from northgate.db.database import Database
from northgate.db.models import (
    ApplicationKey,
    Gateway,
    GatewayPolicy,
    ModelPrice,
    Organization,
    Project,
    ProviderCredential,
    Route,
)


async def bootstrap(settings: Settings) -> None:
    application_digest = settings.application_key_sha256
    provider_api_key = settings.provider_api_key
    encryption_key = settings.credential_encryption_key
    if application_digest is None or not application_digest.get_secret_value():
        raise ValueError("NORTHGATE_APPLICATION_KEY_SHA256 is required")
    if provider_api_key is None or not provider_api_key.get_secret_value():
        raise ValueError("NORTHGATE_PROVIDER_API_KEY is required")
    if encryption_key is None or not encryption_key.get_secret_value():
        raise ValueError("NORTHGATE_CREDENTIAL_ENCRYPTION_KEY is required")

    database = Database(settings.database_url.get_secret_value())
    cipher = CredentialCipher(encryption_key.get_secret_value())
    try:
        async with database.sessions() as session:
            organization = await session.scalar(
                select(Organization).where(Organization.name == "default")
            )
            if organization is None:
                organization = Organization(name="default")
                session.add(organization)
                await session.flush()

            project = await session.scalar(
                select(Project).where(
                    Project.organization_id == organization.id,
                    Project.name == "default",
                )
            )
            if project is None:
                project = Project(organization_id=organization.id, name="default")
                session.add(project)
                await session.flush()

            gateway = await session.scalar(
                select(Gateway).where(
                    Gateway.project_id == project.id,
                    Gateway.slug == settings.gateway_slug,
                )
            )
            if gateway is None:
                gateway = Gateway(project_id=project.id, slug=settings.gateway_slug)
                session.add(gateway)
                await session.flush()

            application_key = await session.scalar(
                select(ApplicationKey).where(
                    ApplicationKey.project_id == project.id,
                    ApplicationKey.name == "bootstrap",
                )
            )
            if application_key is None:
                application_key = ApplicationKey(
                    project_id=project.id,
                    name="bootstrap",
                    key_digest=application_digest.get_secret_value(),
                    allowed_metadata_keys=[
                        key.strip()
                        for key in settings.allowed_metadata_keys.split(",")
                        if key.strip()
                    ],
                )
                session.add(application_key)
            else:
                application_key.key_digest = application_digest.get_secret_value()
                application_key.allowed_metadata_keys = [
                    key.strip() for key in settings.allowed_metadata_keys.split(",") if key.strip()
                ]
                application_key.revoked_at = None

            provider_credential = await session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.project_id == project.id,
                    ProviderCredential.name == "default-openai",
                )
            )
            encrypted_api_key = cipher.encrypt(provider_api_key.get_secret_value())
            if provider_credential is None:
                provider_credential = ProviderCredential(
                    project_id=project.id,
                    name="default-openai",
                    provider="openai",
                    base_url=settings.provider_base_url,
                    encrypted_api_key=encrypted_api_key,
                )
                session.add(provider_credential)
                await session.flush()
            else:
                provider_credential.base_url = settings.provider_base_url
                provider_credential.encrypted_api_key = encrypted_api_key

            route = await session.scalar(
                select(Route).where(
                    Route.gateway_id == gateway.id,
                    Route.name == "default-openai",
                )
            )
            if route is None:
                session.add(
                    Route(
                        gateway_id=gateway.id,
                        provider_credential_id=provider_credential.id,
                        name="default-openai",
                        priority=0,
                        weight=1,
                        match_metadata={},
                        enabled=True,
                        max_retries=settings.provider_max_retries,
                        retry_status_codes=[
                            int(code.strip())
                            for code in settings.provider_retry_status_codes.split(",")
                            if code.strip()
                        ],
                        health_failure_threshold=(
                            settings.route_health_failure_threshold
                            if settings.route_health_enabled
                            else 0
                        ),
                        health_recovery_seconds=settings.route_health_recovery_seconds,
                        health_failure_status_codes=[
                            int(code.strip())
                            for code in settings.route_health_failure_status_codes.split(",")
                            if code.strip()
                        ],
                    )
                )
            else:
                route.provider_credential_id = provider_credential.id
                route.priority = 0
                route.enabled = True
                route.max_retries = settings.provider_max_retries
                route.retry_status_codes = [
                    int(code.strip())
                    for code in settings.provider_retry_status_codes.split(",")
                    if code.strip()
                ]
                route.health_failure_threshold = (
                    settings.route_health_failure_threshold if settings.route_health_enabled else 0
                )
                route.health_recovery_seconds = settings.route_health_recovery_seconds
                route.health_failure_status_codes = [
                    int(code.strip())
                    for code in settings.route_health_failure_status_codes.split(",")
                    if code.strip()
                ]

            fallback_key = settings.fallback_provider_api_key
            if (
                settings.fallback_provider_name
                and settings.fallback_provider_base_url
                and fallback_key is not None
                and fallback_key.get_secret_value()
            ):
                fallback_name = f"fallback-{settings.fallback_provider_name}"
                fallback_credential = await session.scalar(
                    select(ProviderCredential).where(
                        ProviderCredential.project_id == project.id,
                        ProviderCredential.name == fallback_name,
                    )
                )
                encrypted_fallback_key = cipher.encrypt(fallback_key.get_secret_value())
                if fallback_credential is None:
                    fallback_credential = ProviderCredential(
                        project_id=project.id,
                        name=fallback_name,
                        provider=settings.fallback_provider_name,
                        base_url=settings.fallback_provider_base_url,
                        encrypted_api_key=encrypted_fallback_key,
                    )
                    session.add(fallback_credential)
                    await session.flush()
                else:
                    fallback_credential.provider = settings.fallback_provider_name
                    fallback_credential.base_url = settings.fallback_provider_base_url
                    fallback_credential.encrypted_api_key = encrypted_fallback_key

                fallback_route = await session.scalar(
                    select(Route).where(
                        Route.gateway_id == gateway.id,
                        Route.name.startswith("fallback-"),
                    )
                )
                retry_status_codes = [
                    int(code.strip())
                    for code in settings.provider_retry_status_codes.split(",")
                    if code.strip()
                ]
                if fallback_route is None:
                    session.add(
                        Route(
                            gateway_id=gateway.id,
                            provider_credential_id=fallback_credential.id,
                            name=fallback_name,
                            priority=1,
                            weight=1,
                            match_metadata={},
                            enabled=True,
                            max_retries=settings.fallback_provider_max_retries,
                            retry_status_codes=retry_status_codes,
                            health_failure_threshold=(
                                settings.route_health_failure_threshold
                                if settings.route_health_enabled
                                else 0
                            ),
                            health_recovery_seconds=settings.route_health_recovery_seconds,
                            health_failure_status_codes=[
                                int(code.strip())
                                for code in settings.route_health_failure_status_codes.split(",")
                                if code.strip()
                            ],
                        )
                    )
                else:
                    fallback_route.provider_credential_id = fallback_credential.id
                    fallback_route.name = fallback_name
                    fallback_route.priority = 1
                    fallback_route.enabled = True
                    fallback_route.max_retries = settings.fallback_provider_max_retries
                    fallback_route.retry_status_codes = retry_status_codes
                    fallback_route.health_failure_threshold = (
                        settings.route_health_failure_threshold
                        if settings.route_health_enabled
                        else 0
                    )
                    fallback_route.health_recovery_seconds = settings.route_health_recovery_seconds
                    fallback_route.health_failure_status_codes = [
                        int(code.strip())
                        for code in settings.route_health_failure_status_codes.split(",")
                        if code.strip()
                    ]

            policy = await session.scalar(
                select(GatewayPolicy).where(GatewayPolicy.gateway_id == gateway.id)
            )
            configured_limits = (
                settings.request_limit_per_minute,
                settings.concurrency_limit,
                settings.token_limit_per_day,
                settings.daily_spend_limit_microusd,
                settings.monthly_spend_limit_microusd,
                settings.exact_cache_ttl_seconds,
            )
            if any(limit is not None for limit in configured_limits):
                if policy is None:
                    policy = GatewayPolicy(gateway_id=gateway.id)
                    session.add(policy)
                policy.requests_per_minute = settings.request_limit_per_minute
                policy.concurrent_requests = settings.concurrency_limit
                policy.tokens_per_day = settings.token_limit_per_day
                policy.daily_spend_microusd = settings.daily_spend_limit_microusd
                policy.monthly_spend_microusd = settings.monthly_spend_limit_microusd
                policy.exact_cache_ttl_seconds = settings.exact_cache_ttl_seconds

            input_price = settings.input_price_microusd_per_million
            output_price = settings.output_price_microusd_per_million
            if input_price is not None and output_price is not None and settings.price_model:
                effective_from = datetime(1970, 1, 1, tzinfo=UTC)
                model_price = await session.scalar(
                    select(ModelPrice).where(
                        ModelPrice.provider == settings.price_provider,
                        ModelPrice.model == settings.price_model,
                        ModelPrice.effective_from == effective_from,
                    )
                )
                if model_price is None:
                    model_price = ModelPrice(
                        provider=settings.price_provider,
                        model=settings.price_model,
                        effective_from=effective_from,
                        input_microusd_per_million=input_price,
                        output_microusd_per_million=output_price,
                    )
                    session.add(model_price)
                else:
                    model_price.input_microusd_per_million = input_price
                    model_price.output_microusd_per_million = output_price

            await session.commit()
    finally:
        await database.close()


def main() -> None:
    asyncio.run(bootstrap(get_settings()))


if __name__ == "__main__":
    main()
