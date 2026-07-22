# Northgate agent guide

This file is the default entry point for coding agents working in this repository.
Keep it short and factual. Detailed contracts belong under `docs/`.

## Read first

1. Read `docs/current-state.md` for the implemented runtime and known boundaries.
2. Read `docs/README.md` and only the task-specific pages it routes to.
3. For reliability changes, read `docs/known-issues.md` and the settlement section
   of `docs/architecture-review.md`.
4. Treat `docs/development.md` as a historical verification log, not as the
   source of current behavior.

If documentation conflicts with code, migrations, or tests, verify the code and
update the documentation in the same change.

## Product boundary

Northgate is a self-hosted AI request gateway. It owns application authentication,
provider credentials, routing, retry/fallback, policy admission, streaming relay,
usage/cost accounting, and operational telemetry. It is not an agent runtime,
model runtime, account pool, RAG system, or MCP orchestrator.

The implemented data-plane protocol is OpenAI-compatible
`/chat/completions`. Do not infer that proposed endpoints in `docs/api-design.md`
are implemented.

## Critical invariants

- Never log or persist raw application keys, provider credentials, authorization
  headers, prompts, tool payloads, or model output by default. Application keys
  may appear only in their documented one-time issuance response; model output is
  relayed to the authenticated caller.
- Every provider call is a separate `ProviderAttemptRecord`, including failed or
  billable retry/fallback attempts.
- Unknown usage remains unknown. Reconciliation must not invent tokens or cost.
- A stream may retry or fall back only before response bytes reach the client.
- SSE `[DONE]` is terminal even if the upstream socket does not reach EOF.
- Client cancellation must not interrupt terminal accounting or Redis lease
  release.
- A settlement event is complete only after guarded database and policy stages
  complete. A zero-row ledger update is success only for an exact terminal replay.
- Reconciliation must not overwrite records protected by `pending`, `retry`, or
  `processing` settlement events.
- Only server-derived or application-key-fixed metadata may affect trusted route
  selection. Caller metadata is untrusted unless a future signed scheme verifies it.
- PostgreSQL is authoritative durable state. Redis contains policy counters,
  leases, cache, circuit state, and other reconstructible operational state.

## Documentation and engineering rules

- Load only the task-specific pages routed by `docs/README.md`; do not treat the
  complete documentation tree as required context.
- Keep one canonical page for each concept and link to it instead of copying its
  full explanation. Lead with current behavior, then rationale or history.
- Use terminology from `docs/product-scope.md` and label designs as `proposed`,
  `accepted`, `implemented`, or `deprecated`.
- Update the owning document when a contract changes. Add an ADR only for a
  durable decision with meaningful alternatives and tradeoffs.
- Do not add Dayboard-specific domain concepts or agent orchestration behavior to
  Northgate. Integration-specific behavior belongs in the application or `north`.
- Keep the data plane available when the management UI is unavailable.
- Prefer explicit provider adapters over lossy universal request rewriting.

## Code map

| Area | Primary files |
| --- | --- |
| App construction and health | `src/northgate/app.py`, `config.py` |
| Proxy orchestration | `proxy.py` |
| Input and route stages | `proxy_input.py`, `route_planning.py`, `routing.py` |
| Provider execution and streaming | `attempt_execution.py`, `provider_adapters.py`, `stream_relay.py` |
| Terminal accounting | `stream_finalization.py`, `proxy_settlement.py`, `settlement.py`, `reconcile.py` |
| Policy and cache | `policy.py`, `exact_cache.py`, `route_health.py` |
| Durable models and migrations | `src/northgate/db/models.py`, `migrations/versions/` |
| Control and analytics APIs | `control.py`, `analytics.py`, `usage.py`, `pricing.py` |
| Console | `apps/console/` |
| Deployment and recovery | `docker-compose*.yml`, `scripts/`, `docs/operations/` |

## Verification

Use focused tests while iterating, then run the relevant gates before completion:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -m "not integration" -q
```

Real-store tests require migrated PostgreSQL and Redis:

```sh
uv run alembic upgrade head
NORTHGATE_REQUIRE_INTEGRATION_STORES=1 \
  uv run pytest -m integration -q
```

Also validate Compose or console changes when touched:

```sh
docker compose config --quiet
cd apps/console && npm run check && npm run build
```

Do not make integration tests pass by allowing required stores to skip in CI.
Preserve unrelated worktree changes and never commit secrets or production payloads.
