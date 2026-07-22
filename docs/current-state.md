# Current implementation state

Status: implemented snapshot  
Last reviewed: 2026-07-22

This page is the shortest authoritative description of what the current codebase
does. It deliberately separates implemented behavior from accepted or proposed
work. Use the source, migrations, and tests when a detail must be exact.

## Product and protocol

Northgate is a self-hosted AI gateway between applications and model providers.
It currently implements one data-plane protocol:

```text
POST /v1/gateways/{gateway_slug}/openai/chat/completions
```

Streaming and non-streaming OpenAI-compatible chat completions are supported,
including tool calls and provider-reported usage. Responses API, Anthropic
Messages, embeddings, image, audio, MCP, RAG, and agent execution are not
implemented data-plane protocols.

Provider adapters currently cover OpenAI-compatible and Azure OpenAI request
construction. Northgate preserves provider differences where they affect URL,
authentication, retry, usage, or error behavior.

## Runtime entry points

| Command | Current responsibility |
| --- | --- |
| `northgate` | FastAPI data plane, control API, analytics API, health, metrics, and console |
| `northgate-worker` | Settlement retry, one-shot drain, heartbeat check, and completed-event cleanup |
| `northgate-reconcile` | Preview or mark stale unprotected ledger records and release stale/expired leases |
| `northgate-bootstrap` | Idempotently create the initial database-backed gateway configuration |
| `northgate-verify` | Check non-streaming, SSE, and tool-call compatibility without logging content |
| `northgate-inspect` | Inspect correlated runs, requests, and stale settlement state through the Operator API |

The data plane and control plane still run in one FastAPI process. The settlement
worker is independently deployable. Separate data/control binaries and versioned
in-memory gateway configuration snapshots are not implemented.

The joined diagnostics service and Operator REST endpoints can inspect one
request or correlate bounded request sets by allowlisted metadata. They return a
versioned shape containing request, attempt, redacted settlement state,
aggregates, metadata trust, and stable findings. `northgate-inspect` exposes the
correlated-run, individual-request, and stale-settlement contracts with JSON or
human output. Stale diagnostics distinguish records protected by a recoverable
settlement event, unprotected records, request-only/attempt-only inconsistencies,
and active or expired Redis concurrency leases. The read-only operator MCP server
is not implemented.

## Request path

```text
request ID and application authentication
  -> bounded body and metadata parsing
  -> database/configuration route resolution
  -> trusted route planning
  -> price lookup and policy admission
  -> RequestRecord creation
  -> provider attempt loop
  -> non-streaming response or SSE relay
  -> durable settlement handoff
  -> inline fast-path processing or worker recovery
```

`proxy_chat_completions()` remains the top-level orchestrator. Input parsing,
route planning, provider transport, stream relay, stream finalization, and
settlement helpers have been extracted, but cache, pricing, admission, attempt
planning, and response construction are still coordinated in `proxy.py`.

Retry and fallback are allowed only before response bytes are committed to the
client. Every actual provider call receives its own attempt ledger record. An
invalid fallback adapter is isolated until that route is reached and cannot block
a healthy primary route.

Request bodies are bounded by `NORTHGATE_MAX_REQUEST_BODY_BYTES`, including
chunked bodies. SSE `[DONE]` terminates relay processing without waiting for
upstream EOF. Finalization creates the durable settlement event before upstream
close, cache, and route-health side effects. It runs in an independent task and
delays direct task-cancellation propagation until accounting completes.

## Settlement and recovery

Durable settlement is optional and defaults off through
`NORTHGATE_SETTLEMENT_OUTBOX_ENABLED=false`. When enabled:

1. The request process inserts a PostgreSQL `SettlementEvent`.
2. It immediately attempts the event once to release leases without worker poll
   latency.
3. Failed stages remain `retry` and are recovered by `northgate-worker` using
   `FOR UPDATE SKIP LOCKED`.
4. Database and Redis policy progress are recorded independently and applied
   idempotently.
5. Missing or conflicting ledger records do not silently complete an event.

Settlement payload schema version is `1`. Migration `0016` backfills historical
events and adds the partial worker-queue index. Completed events can be deleted in
bounded batches with:

```sh
northgate-worker --cleanup-completed --retention-days 30 --cleanup-batch-size 1000
```

The command never deletes pending, retrying, processing, or failed events.

`northgate-reconcile` excludes records that have a `pending`, `retry`, or
`processing` event. Unprotected stale records may be marked
`settlement_incomplete`; their unknown token or cost values remain unknown.

## Readiness

PostgreSQL or required Redis failure returns `503`. With outbox enabled and no
worker heartbeat:

- no recoverable backlog or a fresh backlog returns `200`, `status: ready`, and
  `degraded: true`;
- an oldest recoverable event beyond
  `NORTHGATE_SETTLEMENT_READINESS_MAX_PENDING_AGE_SECONDS` returns `503`;
- inability to inspect the backlog returns `503`.

The default backlog threshold is 300 seconds. Worker heartbeat absence remains an
independent Prometheus warning.

## State ownership

PostgreSQL owns organizations, projects, gateways, application-key digests,
encrypted provider credentials, routes, policies, prices, request records,
attempt records, metadata trust, and settlement events.

Redis owns rate windows, token/spend reservations, concurrency leases and their
start metadata, exact cache entries, circuit-breaker state, and worker heartbeat.
Policy-controlled traffic fails closed when required Redis state is unavailable.

Database routing still resolves configuration and decrypts provider credentials
on the request path. Versioned in-memory configuration snapshots are planned but
not implemented.

## Metadata trust

New application keys default to trusted routing mode. Route selection can consume:

- `northgate.project_id` and `northgate.application_id`, derived by the server;
- operator-configured `fixed_metadata` bound to the application key.

Caller-provided allowlisted metadata is stored as untrusted correlation data and
does not affect trusted route selection. Historical keys may remain in explicit
`legacy` mode and must be replaced before caller-controlled routing is removed.
Signed dynamic metadata is not implemented.

Request records preserve `server`, `fixed`, `untrusted`, and `legacy` trust
classes. Tenant aggregation consumes trusted values only.

## Policy and routing scope

Implemented limits are gateway-scoped request rate, concurrency, daily tokens,
daily spend, and monthly spend. Exact cache TTL is also gateway policy. Generic
organization/project/application/tenant/user multi-level policy subjects are not
implemented.

Routing supports ordered priorities, bounded retries, fallback, deterministic
weights, trusted exact metadata matching, circuit breakers, and exact caching.
Budget admission still conservatively reserves for the configured attempt plan;
incremental reservation per retry/fallback is future work.

## Verification baseline

CI has separate jobs:

- quality: Ruff, formatting, non-integration tests, and Compose validation;
- integration: PostgreSQL 17, Redis 8, Alembic upgrade, and tests marked
  `integration` with store failures configured to fail rather than skip.

At this review, migration `0016` is the single Alembic head. The local suite has
87 non-integration and 9 real-store integration tests. Counts are a snapshot, not
a contract; new behavior should add proportional coverage.

## Open work

The next architectural work is intentionally narrower than provider expansion:

1. finish trusted metadata migration with signed dynamic values, audit, and
   removal of legacy route matching;
2. continue reducing orchestration inside `proxy_chat_completions()` using plain
   functions and immutable context data;
3. compile and atomically swap versioned gateway configuration snapshots;
4. add independent data-plane/control-plane entry points;
5. add application and trusted-tenant policy subjects before a generic hierarchy;
6. connect production heartbeat/backlog alerts and complete production-like soak
   closure criteria;
7. deploy and verify the direct-cancellation settlement fix, then expose the
   diagnostics contract through an independently deployable read-only MCP
   adapter.

See `known-issues.md` for active reliability closure criteria and `roadmap.md` for
milestone ordering.
