from dataclasses import replace

from northgate.routing import PolicyLimits, ResolvedRoute, select_routes


def _route(
    provider: str,
    *,
    priority: int = 0,
    weight: int = 1,
    match_metadata: tuple[tuple[str, str], ...] = (),
) -> ResolvedRoute:
    return ResolvedRoute(
        project_id=None,
        gateway_id=None,
        route_id=None,
        provider=provider,
        base_url=f"https://{provider}.test/v1",
        api_key="secret",
        allowed_metadata_keys=frozenset({"tenant_id", "environment"}),
        policy=PolicyLimits(),
        priority=priority,
        weight=weight,
        match_metadata=match_metadata,
    )


def test_metadata_specificity_orders_routes_without_discarding_generic_fallbacks() -> None:
    routes = [
        _route("generic"),
        _route("tenant-a", match_metadata=(("tenant_id", "a"),)),
        _route("tenant-b", match_metadata=(("tenant_id", "b"),)),
        _route("fallback", priority=1),
    ]

    selected = select_routes(routes, {"tenant_id": "a"}, "req_metadata")

    assert [route.provider for route in selected] == ["tenant-a", "generic", "fallback"]
    assert [route.provider for route in select_routes(routes, {}, "req_generic")] == [
        "generic",
        "fallback",
    ]


def test_weighted_selection_is_stable_and_distributed() -> None:
    light = _route("light", weight=1)
    heavy = replace(light, provider="heavy", base_url="https://heavy.test/v1", weight=3)
    routes = [light, heavy]

    selections = [select_routes(routes, {}, f"req_{index}")[0].provider for index in range(1000)]

    assert selections.count("heavy") in range(700, 801)
    assert select_routes(routes, {}, "req_stable") == select_routes(routes, {}, "req_stable")


def test_no_metadata_match_returns_no_route() -> None:
    routes = [_route("tenant-a", match_metadata=(("tenant_id", "a"),))]

    assert select_routes(routes, {"tenant_id": "b"}, "req_no_match") == []
