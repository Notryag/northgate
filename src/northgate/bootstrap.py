import asyncio

from sqlalchemy import select

from northgate.config import Settings, get_settings
from northgate.credentials import CredentialCipher
from northgate.db.database import Database
from northgate.db.models import (
    ApplicationKey,
    Gateway,
    GatewayPolicy,
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
                select(Route).where(Route.gateway_id == gateway.id, Route.priority == 0)
            )
            if route is None:
                session.add(
                    Route(
                        gateway_id=gateway.id,
                        provider_credential_id=provider_credential.id,
                        name="default-openai",
                        priority=0,
                        enabled=True,
                    )
                )
            else:
                route.provider_credential_id = provider_credential.id
                route.enabled = True

            policy = await session.scalar(
                select(GatewayPolicy).where(GatewayPolicy.gateway_id == gateway.id)
            )
            configured_limits = (
                settings.request_limit_per_minute,
                settings.concurrency_limit,
                settings.token_limit_per_day,
            )
            if any(limit is not None for limit in configured_limits):
                if policy is None:
                    policy = GatewayPolicy(gateway_id=gateway.id)
                    session.add(policy)
                policy.requests_per_minute = settings.request_limit_per_minute
                policy.concurrent_requests = settings.concurrency_limit
                policy.tokens_per_day = settings.token_limit_per_day

            await session.commit()
    finally:
        await database.close()


def main() -> None:
    asyncio.run(bootstrap(get_settings()))


if __name__ == "__main__":
    main()
