import re
from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import AnyHttpUrl, BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
from northgate.operator_auth import authorize_operator

router = APIRouter(prefix="/api/v1")

_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$")
_METADATA_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RETRY_STATUS_CODES = [429, 500, 502, 503, 504]
_HEALTH_STATUS_CODES = [500, 502, 503, 504]


class NamedResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectCreate(NamedResourceCreate):
    organization_id: UUID


class GatewayCreate(BaseModel):
    project_id: UUID
    slug: str = Field(min_length=1, max_length=120)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not _SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must contain lowercase letters, digits, or internal hyphens")
        return value


class ApplicationKeyCreate(NamedResourceCreate):
    project_id: UUID
    allowed_metadata_keys: list[str] = Field(default_factory=list, max_length=32)
    fixed_metadata: dict[str, str] = Field(default_factory=dict)
    metadata_routing_mode: Literal["trusted", "legacy"] = "trusted"

    @field_validator("allowed_metadata_keys")
    @classmethod
    def validate_metadata_keys(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not all(
            _METADATA_KEY_PATTERN.fullmatch(key) for key in value
        ):
            raise ValueError(
                "metadata keys must be unique and contain only letters, digits, _, ., or -"
            )
        return value

    @field_validator("fixed_metadata")
    @classmethod
    def validate_fixed_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 16 or any(
            not _METADATA_KEY_PATTERN.fullmatch(key)
            or key.startswith("northgate.")
            or len(item) > 256
            for key, item in value.items()
        ):
            raise ValueError("fixed_metadata is too large or contains an invalid key or value")
        return value

    @model_validator(mode="after")
    def metadata_classes_do_not_overlap(self) -> "ApplicationKeyCreate":
        overlap = set(self.allowed_metadata_keys) & self.fixed_metadata.keys()
        if overlap:
            raise ValueError("fixed_metadata keys cannot also accept caller-provided values")
        return self


class ProviderCredentialCreate(NamedResourceCreate):
    project_id: UUID
    provider: str = Field(default="openai", min_length=1, max_length=40)
    base_url: AnyHttpUrl
    adapter: Literal["openai_compatible", "azure_openai"] = "openai_compatible"
    adapter_config: dict[str, str] = Field(default_factory=dict)
    api_key: str = Field(min_length=1, max_length=8192)

    @field_validator("adapter_config")
    @classmethod
    def validate_adapter_config(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 16 or any(len(key) > 64 or len(item) > 512 for key, item in value.items()):
            raise ValueError("adapter_config is too large")
        return value

    @field_validator("api_key")
    @classmethod
    def reject_blank_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank")
        return value


class ProviderCredentialSecretUpdate(BaseModel):
    api_key: str = Field(min_length=1, max_length=8192)

    @field_validator("api_key")
    @classmethod
    def reject_blank_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank")
        return value


class RouteCreate(NamedResourceCreate):
    gateway_id: UUID
    provider_credential_id: UUID
    priority: int = Field(default=0, ge=0, le=10000)
    weight: int = Field(default=1, ge=1, le=10000)
    match_metadata: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    max_retries: int = Field(default=0, ge=0, le=5)
    retry_status_codes: list[int] = Field(
        default_factory=lambda: list(_RETRY_STATUS_CODES), max_length=16
    )
    health_failure_threshold: int = Field(default=0, ge=0, le=100)
    health_recovery_seconds: int = Field(default=30, ge=1, le=3600)
    health_failure_status_codes: list[int] = Field(
        default_factory=lambda: list(_HEALTH_STATUS_CODES), max_length=16
    )
    default_max_output_tokens: int | None = Field(default=None, ge=1, le=2_000_000)

    @field_validator("match_metadata")
    @classmethod
    def validate_match_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 16 or any(
            not _METADATA_KEY_PATTERN.fullmatch(key) or len(item) > 256
            for key, item in value.items()
        ):
            raise ValueError("match_metadata is too large or contains an invalid key")
        return value

    @field_validator("retry_status_codes", "health_failure_status_codes")
    @classmethod
    def validate_status_codes(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(code < 400 or code > 599 for code in value):
            raise ValueError("status codes must be unique HTTP error status codes")
        return value


class RouteUpdate(BaseModel):
    priority: int | None = Field(default=None, ge=0, le=10000)
    weight: int | None = Field(default=None, ge=1, le=10000)
    enabled: bool | None = None
    default_max_output_tokens: int | None = Field(default=None, ge=1, le=2_000_000)


class GatewayPolicyReplace(BaseModel):
    requests_per_minute: int | None = Field(ge=1, le=10_000_000)
    concurrent_requests: int | None = Field(ge=1, le=100_000)
    tokens_per_day: int | None = Field(ge=1, le=2_000_000_000)
    daily_spend_microusd: int | None = Field(ge=1, le=9_000_000_000_000_000)
    monthly_spend_microusd: int | None = Field(ge=1, le=9_000_000_000_000_000)
    exact_cache_ttl_seconds: int | None = Field(ge=1, le=86_400)


class ModelPriceCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=200)
    effective_from: datetime
    input_microusd_per_million: int = Field(ge=0, le=9_000_000_000_000_000)
    output_microusd_per_million: int = Field(ge=0, le=9_000_000_000_000_000)

    @field_validator("provider", "model")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("effective_from")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("effective_from must include a timezone")
        return value


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def _json(content: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(jsonable_encoder(content), status_code=status_code)


def _database(request: Request) -> Database | None:
    return request.app.state.database


def _cipher(request: Request) -> CredentialCipher | None:
    return request.app.state.credential_cipher


def _preflight(request: Request, *, credential_write: bool = False) -> Response | None:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    if _database(request) is None:
        return _error("CONTROL_PLANE_UNAVAILABLE", "Control plane unavailable", 503)
    if credential_write and _cipher(request) is None:
        return _error(
            "CREDENTIAL_ENCRYPTION_UNAVAILABLE",
            "Provider credential encryption unavailable",
            503,
        )
    return None


def _common(
    resource: Organization
    | Project
    | Gateway
    | ApplicationKey
    | ProviderCredential
    | Route
    | GatewayPolicy
    | ModelPrice,
) -> dict[str, object]:
    return {
        "id": resource.id,
        "created_at": resource.created_at,
    }


@router.get("/organizations")
async def list_organizations(request: Request) -> Response:
    if error := _preflight(request):
        return error
    async with _database(request).sessions() as session:  # type: ignore[union-attr]
        resources = (
            await session.scalars(select(Organization).order_by(Organization.created_at))
        ).all()
    return _json([{**_common(item), "name": item.name} for item in resources])


@router.post("/organizations")
async def create_organization(request: Request, payload: NamedResourceCreate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        if await session.scalar(select(Organization.id).where(Organization.name == payload.name)):
            return _error("RESOURCE_CONFLICT", "Organization name already exists", 409)
        resource = Organization(name=payload.name)
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
    return _json({**_common(resource), "name": resource.name}, 201)


@router.get("/projects")
async def list_projects(request: Request, organization_id: UUID | None = None) -> Response:
    if error := _preflight(request):
        return error
    statement = select(Project).order_by(Project.created_at)
    if organization_id is not None:
        statement = statement.where(Project.organization_id == organization_id)
    async with _database(request).sessions() as session:  # type: ignore[union-attr]
        resources = (await session.scalars(statement)).all()
    return _json(
        [
            {**_common(item), "organization_id": item.organization_id, "name": item.name}
            for item in resources
        ]
    )


@router.post("/projects")
async def create_project(request: Request, payload: ProjectCreate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        if await session.get(Organization, payload.organization_id) is None:
            return _error("ORGANIZATION_NOT_FOUND", "Organization not found", 404)
        if await session.scalar(
            select(Project.id).where(
                Project.organization_id == payload.organization_id, Project.name == payload.name
            )
        ):
            return _error("RESOURCE_CONFLICT", "Project name already exists", 409)
        resource = Project(organization_id=payload.organization_id, name=payload.name)
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
    return _json(
        {**_common(resource), "organization_id": resource.organization_id, "name": resource.name},
        201,
    )


@router.get("/gateways")
async def list_gateways(request: Request, project_id: UUID | None = None) -> Response:
    if error := _preflight(request):
        return error
    statement = select(Gateway).order_by(Gateway.created_at)
    if project_id is not None:
        statement = statement.where(Gateway.project_id == project_id)
    async with _database(request).sessions() as session:  # type: ignore[union-attr]
        resources = (await session.scalars(statement)).all()
    return _json(
        [{**_common(item), "project_id": item.project_id, "slug": item.slug} for item in resources]
    )


@router.post("/gateways")
async def create_gateway(request: Request, payload: GatewayCreate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        if await session.get(Project, payload.project_id) is None:
            return _error("PROJECT_NOT_FOUND", "Project not found", 404)
        if await session.scalar(
            select(Gateway.id).where(
                Gateway.project_id == payload.project_id, Gateway.slug == payload.slug
            )
        ):
            return _error("RESOURCE_CONFLICT", "Gateway slug already exists", 409)
        resource = Gateway(project_id=payload.project_id, slug=payload.slug)
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
    return _json(
        {**_common(resource), "project_id": resource.project_id, "slug": resource.slug}, 201
    )


@router.get("/application-keys")
async def list_application_keys(request: Request, project_id: UUID | None = None) -> Response:
    if error := _preflight(request):
        return error
    statement = select(ApplicationKey).order_by(ApplicationKey.created_at)
    if project_id is not None:
        statement = statement.where(ApplicationKey.project_id == project_id)
    async with _database(request).sessions() as session:  # type: ignore[union-attr]
        resources = (await session.scalars(statement)).all()
    return _json(
        [
            {
                **_common(item),
                "project_id": item.project_id,
                "name": item.name,
                "allowed_metadata_keys": item.allowed_metadata_keys,
                "fixed_metadata": item.fixed_metadata,
                "metadata_routing_mode": item.metadata_routing_mode,
                "revoked_at": item.revoked_at,
            }
            for item in resources
        ]
    )


@router.post("/application-keys")
async def create_application_key(request: Request, payload: ApplicationKeyCreate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    plaintext = f"ng_live_{token_urlsafe(32)}"
    resource = ApplicationKey(
        project_id=payload.project_id,
        name=payload.name,
        key_digest=sha256(plaintext.encode()).hexdigest(),
        allowed_metadata_keys=payload.allowed_metadata_keys,
        fixed_metadata=payload.fixed_metadata,
        metadata_routing_mode=payload.metadata_routing_mode,
    )
    async with database.sessions() as session:
        if await session.get(Project, payload.project_id) is None:
            return _error("PROJECT_NOT_FOUND", "Project not found", 404)
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
    return _json(
        {
            **_common(resource),
            "project_id": resource.project_id,
            "name": resource.name,
            "allowed_metadata_keys": resource.allowed_metadata_keys,
            "fixed_metadata": resource.fixed_metadata,
            "metadata_routing_mode": resource.metadata_routing_mode,
            "key": plaintext,
        },
        201,
    )


@router.post("/application-keys/{key_id}/revoke")
async def revoke_application_key(request: Request, key_id: UUID) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        resource = await session.get(ApplicationKey, key_id)
        if resource is None:
            return _error("APPLICATION_KEY_NOT_FOUND", "Application key not found", 404)
        if resource.revoked_at is None:
            resource.revoked_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(resource)
    return _json({**_common(resource), "revoked_at": resource.revoked_at})


@router.get("/provider-credentials")
async def list_provider_credentials(request: Request, project_id: UUID | None = None) -> Response:
    if error := _preflight(request):
        return error
    statement = select(ProviderCredential).order_by(ProviderCredential.created_at)
    if project_id is not None:
        statement = statement.where(ProviderCredential.project_id == project_id)
    async with _database(request).sessions() as session:  # type: ignore[union-attr]
        resources = (await session.scalars(statement)).all()
    return _json(
        [
            {
                **_common(item),
                "project_id": item.project_id,
                "name": item.name,
                "provider": item.provider,
                "base_url": item.base_url,
                "adapter": item.adapter,
                "adapter_config": item.adapter_config,
            }
            for item in resources
        ]
    )


@router.post("/provider-credentials")
async def create_provider_credential(
    request: Request, payload: ProviderCredentialCreate
) -> Response:
    if error := _preflight(request, credential_write=True):
        return error
    database = _database(request)
    cipher = _cipher(request)
    assert database is not None and cipher is not None
    async with database.sessions() as session:
        if await session.get(Project, payload.project_id) is None:
            return _error("PROJECT_NOT_FOUND", "Project not found", 404)
        if await session.scalar(
            select(ProviderCredential.id).where(
                ProviderCredential.project_id == payload.project_id,
                ProviderCredential.name == payload.name,
            )
        ):
            return _error("RESOURCE_CONFLICT", "Provider credential name already exists", 409)
        resource = ProviderCredential(
            project_id=payload.project_id,
            name=payload.name,
            provider=payload.provider,
            base_url=str(payload.base_url).rstrip("/"),
            adapter=payload.adapter,
            adapter_config=payload.adapter_config,
            encrypted_api_key=cipher.encrypt(payload.api_key),
        )
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
    return _json(
        {
            **_common(resource),
            "project_id": resource.project_id,
            "name": resource.name,
            "provider": resource.provider,
            "base_url": resource.base_url,
            "adapter": resource.adapter,
            "adapter_config": resource.adapter_config,
        },
        201,
    )


@router.put("/provider-credentials/{credential_id}/secret")
async def update_provider_credential_secret(
    request: Request, credential_id: UUID, payload: ProviderCredentialSecretUpdate
) -> Response:
    if error := _preflight(request, credential_write=True):
        return error
    database = _database(request)
    cipher = _cipher(request)
    assert database is not None and cipher is not None
    async with database.sessions() as session:
        resource = await session.get(ProviderCredential, credential_id)
        if resource is None:
            return _error("PROVIDER_CREDENTIAL_NOT_FOUND", "Provider credential not found", 404)
        resource.encrypted_api_key = cipher.encrypt(payload.api_key)
        await session.commit()
    return Response(status_code=204)


@router.get("/routes")
async def list_routes(request: Request, gateway_id: UUID | None = None) -> Response:
    if error := _preflight(request):
        return error
    statement = select(Route).order_by(Route.gateway_id, Route.priority, Route.created_at)
    if gateway_id is not None:
        statement = statement.where(Route.gateway_id == gateway_id)
    async with _database(request).sessions() as session:  # type: ignore[union-attr]
        resources = (await session.scalars(statement)).all()
    return _json(
        [
            {
                **_common(item),
                "gateway_id": item.gateway_id,
                "provider_credential_id": item.provider_credential_id,
                "name": item.name,
                "priority": item.priority,
                "weight": item.weight,
                "match_metadata": item.match_metadata,
                "enabled": item.enabled,
                "max_retries": item.max_retries,
                "retry_status_codes": item.retry_status_codes,
                "health_failure_threshold": item.health_failure_threshold,
                "health_recovery_seconds": item.health_recovery_seconds,
                "health_failure_status_codes": item.health_failure_status_codes,
                "default_max_output_tokens": item.default_max_output_tokens,
            }
            for item in resources
        ]
    )


@router.post("/routes")
async def create_route(request: Request, payload: RouteCreate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        gateway = await session.get(Gateway, payload.gateway_id)
        if gateway is None:
            return _error("GATEWAY_NOT_FOUND", "Gateway not found", 404)
        credential = await session.get(ProviderCredential, payload.provider_credential_id)
        if credential is None:
            return _error("PROVIDER_CREDENTIAL_NOT_FOUND", "Provider credential not found", 404)
        if gateway.project_id != credential.project_id:
            return _error(
                "CROSS_PROJECT_ROUTE",
                "Gateway and provider credential must belong to the same project",
                409,
            )
        if await session.scalar(
            select(Route.id).where(
                Route.gateway_id == payload.gateway_id, Route.name == payload.name
            )
        ):
            return _error("RESOURCE_CONFLICT", "Route name already exists", 409)
        resource = Route(**payload.model_dump())
        session.add(resource)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return _error("RESOURCE_CONFLICT", "Route conflicts with existing configuration", 409)
        await session.refresh(resource)
    return _json(
        {
            **_common(resource),
            "gateway_id": resource.gateway_id,
            "provider_credential_id": resource.provider_credential_id,
            "name": resource.name,
            "priority": resource.priority,
            "weight": resource.weight,
            "match_metadata": resource.match_metadata,
            "enabled": resource.enabled,
            "default_max_output_tokens": resource.default_max_output_tokens,
        },
        201,
    )


@router.patch("/routes/{route_id}")
async def update_route(request: Request, route_id: UUID, payload: RouteUpdate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    changes = payload.model_dump(exclude_none=True)
    if "default_max_output_tokens" in payload.model_fields_set:
        changes["default_max_output_tokens"] = payload.default_max_output_tokens
    if not changes:
        return _error("EMPTY_UPDATE", "At least one route field is required", 400)
    async with database.sessions() as session:
        resource = await session.get(Route, route_id)
        if resource is None:
            return _error("ROUTE_NOT_FOUND", "Route not found", 404)
        for key, value in changes.items():
            setattr(resource, key, value)
        await session.commit()
        await session.refresh(resource)
    return _json(
        {
            **_common(resource),
            "priority": resource.priority,
            "weight": resource.weight,
            "enabled": resource.enabled,
            "default_max_output_tokens": resource.default_max_output_tokens,
        }
    )


def _model_price(resource: ModelPrice) -> dict[str, object]:
    return {
        **_common(resource),
        "provider": resource.provider,
        "model": resource.model,
        "effective_from": resource.effective_from,
        "input_microusd_per_million": resource.input_microusd_per_million,
        "output_microusd_per_million": resource.output_microusd_per_million,
    }


@router.get("/model-prices")
async def list_model_prices(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
) -> Response:
    if error := _preflight(request):
        return error
    statement = select(ModelPrice)
    if provider is not None:
        statement = statement.where(ModelPrice.provider == provider)
    if model is not None:
        statement = statement.where(ModelPrice.model == model)
    statement = statement.order_by(
        ModelPrice.provider,
        ModelPrice.model,
        ModelPrice.effective_from.desc(),
    )
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        resources = (await session.scalars(statement)).all()
    return _json([_model_price(item) for item in resources])


@router.post("/model-prices")
async def create_model_price(request: Request, payload: ModelPriceCreate) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        if await session.scalar(
            select(ModelPrice.id).where(
                ModelPrice.provider == payload.provider,
                ModelPrice.model == payload.model,
                ModelPrice.effective_from == payload.effective_from,
            )
        ):
            return _error("RESOURCE_CONFLICT", "Model price already exists", 409)
        resource = ModelPrice(**payload.model_dump())
        session.add(resource)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return _error("RESOURCE_CONFLICT", "Model price already exists", 409)
        await session.refresh(resource)
    return _json(_model_price(resource), 201)


def _policy(resource: GatewayPolicy) -> dict[str, object]:
    return {
        **_common(resource),
        "gateway_id": resource.gateway_id,
        "requests_per_minute": resource.requests_per_minute,
        "concurrent_requests": resource.concurrent_requests,
        "tokens_per_day": resource.tokens_per_day,
        "daily_spend_microusd": resource.daily_spend_microusd,
        "monthly_spend_microusd": resource.monthly_spend_microusd,
        "exact_cache_ttl_seconds": resource.exact_cache_ttl_seconds,
    }


@router.get("/policies")
async def list_policies(request: Request, gateway_id: UUID | None = None) -> Response:
    if error := _preflight(request):
        return error
    statement = select(GatewayPolicy).order_by(GatewayPolicy.created_at)
    if gateway_id is not None:
        statement = statement.where(GatewayPolicy.gateway_id == gateway_id)
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        resources = (await session.scalars(statement)).all()
    return _json([_policy(item) for item in resources])


@router.put("/policies/{gateway_id}")
async def replace_policy(
    request: Request, gateway_id: UUID, payload: GatewayPolicyReplace
) -> Response:
    if error := _preflight(request):
        return error
    database = _database(request)
    assert database is not None
    async with database.sessions() as session:
        if await session.get(Gateway, gateway_id) is None:
            return _error("GATEWAY_NOT_FOUND", "Gateway not found", 404)
        resource = await session.scalar(
            select(GatewayPolicy).where(GatewayPolicy.gateway_id == gateway_id)
        )
        created = resource is None
        if resource is None:
            resource = GatewayPolicy(gateway_id=gateway_id, **payload.model_dump())
            session.add(resource)
        else:
            for key, value in payload.model_dump().items():
                setattr(resource, key, value)
        await session.commit()
        await session.refresh(resource)
    return _json(_policy(resource), 201 if created else 200)
