from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from northgate.db.base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))


class Gateway(TimestampMixin, Base):
    __tablename__ = "gateways"
    __table_args__ = (UniqueConstraint("project_id", "slug"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(120))


class ApplicationKey(TimestampMixin, Base):
    __tablename__ = "application_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    key_digest: Mapped[str] = mapped_column(String(64), unique=True)
    allowed_metadata_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderCredential(TimestampMixin, Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(40), default="openai")
    base_url: Mapped[str] = mapped_column(String(2048))
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary)


class Route(TimestampMixin, Base):
    __tablename__ = "routes"
    __table_args__ = (UniqueConstraint("gateway_id", "priority"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    gateway_id: Mapped[UUID] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), index=True
    )
    provider_credential_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(default=True)

    provider_credential: Mapped[ProviderCredential] = relationship(lazy="joined")


class GatewayPolicy(TimestampMixin, Base):
    __tablename__ = "gateway_policies"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    gateway_id: Mapped[UUID] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), unique=True
    )
    requests_per_minute: Mapped[int | None] = mapped_column(Integer)
    concurrent_requests: Mapped[int | None] = mapped_column(Integer)
    tokens_per_day: Mapped[int | None] = mapped_column(Integer)
    daily_spend_microusd: Mapped[int | None] = mapped_column(BigInteger)
    monthly_spend_microusd: Mapped[int | None] = mapped_column(BigInteger)


class ModelPrice(TimestampMixin, Base):
    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("provider", "model", "effective_from"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    input_microusd_per_million: Mapped[int] = mapped_column(BigInteger)
    output_microusd_per_million: Mapped[int] = mapped_column(BigInteger)


class RequestRecord(Base):
    __tablename__ = "request_records"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    gateway_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("gateways.id", ondelete="SET NULL"), index=True
    )
    route_id: Mapped[UUID | None] = mapped_column(ForeignKey("routes.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(200))
    request_metadata: Mapped[dict[str, str] | None] = mapped_column(JSON)
    price_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_prices.id", ondelete="SET NULL")
    )
    cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(40), default="started")
    http_status: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
