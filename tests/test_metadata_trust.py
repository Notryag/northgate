import pytest
from pydantic import ValidationError

from northgate.control import ApplicationKeyCreate
from northgate.route_planning import plan_routes
from northgate.routing import PolicyLimits, ResolvedRoute


def _route(
    provider: str,
    *,
    match_metadata: tuple[tuple[str, str], ...] = (),
    trusted_metadata: tuple[tuple[str, str], ...] = (),
    metadata_routing_mode: str = "trusted",
) -> ResolvedRoute:
    return ResolvedRoute(
        project_id=None,
        gateway_id=None,
        route_id=None,
        provider=provider,
        base_url=f"https://{provider}.test/v1",
        api_key="secret",
        allowed_metadata_keys=frozenset({"tenant_id", "run_id"}),
        policy=PolicyLimits(),
        match_metadata=match_metadata,
        trusted_metadata=trusted_metadata,
        metadata_routing_mode=metadata_routing_mode,
    )


def test_route_planning_uses_only_key_bound_metadata() -> None:
    trusted = (("tenant_id", "b"),)
    routes = [
        _route("tenant-a", match_metadata=(("tenant_id", "a"),), trusted_metadata=trusted),
        _route("tenant-b", match_metadata=(("tenant_id", "b"),), trusted_metadata=trusted),
    ]

    selected = plan_routes(routes, "req_trusted_metadata", {"tenant_id": "a"})

    assert [route.provider for route in selected] == ["tenant-b"]


def test_legacy_key_preserves_caller_metadata_during_migration() -> None:
    routes = [
        _route(
            "tenant-a",
            match_metadata=(("tenant_id", "a"),),
            metadata_routing_mode="legacy",
        ),
        _route(
            "tenant-b",
            match_metadata=(("tenant_id", "b"),),
            metadata_routing_mode="legacy",
        ),
    ]

    selected = plan_routes(routes, "req_legacy_metadata", {"tenant_id": "a"})

    assert [route.provider for route in selected] == ["tenant-a"]


@pytest.mark.parametrize(
    "fixed_metadata",
    [
        {"tenant_id": "fixed"},
        {"northgate.application_id": "forged"},
    ],
)
def test_application_key_rejects_overlapping_or_reserved_fixed_metadata(
    fixed_metadata: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        ApplicationKeyCreate(
            project_id="00000000-0000-0000-0000-000000000001",
            name="metadata-key",
            allowed_metadata_keys=["tenant_id"],
            fixed_metadata=fixed_metadata,
        )
