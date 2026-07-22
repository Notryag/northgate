# Known issues and hardening work

Status: active  
Last reviewed: 2026-07-22

This page tracks reliability gaps that remain open after an immediate incident
fix. An item is not closed merely because production traffic recovered; it is
closed only when its prevention, detection, and recovery criteria are verified.

The broader sequencing and accepted tradeoffs are recorded in the
[architecture review](architecture-review.md).

## Caller metadata values are not bound to application identity

Status: fixed-value binding implemented; legacy migration and signed metadata remain open

Trusted application keys separate caller-allowed correlation metadata from
operator-configured fixed routing values. The proxy validates caller metadata but
route selection consumes only fixed values and server-derived project/application
identity. Pre-existing keys remain in explicit `legacy` mode during migration and
may still route on caller values; those values are cooperative attribution, not
proof of tenant, environment, privilege, or data region.

Operators must not use legacy caller metadata for authorization-sensitive routes,
privileged models, higher budgets, regulated regions, or production environments.

Implemented on 2026-07-22:

- Revision `0014` adds key-bound `fixed_metadata` and a routing mode to application
  keys. It backfills existing keys as `legacy`; newly issued keys default to
  `trusted`.
- Trusted route planning reads only server-derived project/application IDs and
  operator-configured fixed values. Caller metadata has no route-selection input
  for a trusted key.
- Control validation prevents fixed keys from overlapping caller-allowed keys and
  reserves the `northgate.*` namespace for server identity.
- Negative tests prove a caller claiming another tenant cannot override a trusted
  key and that legacy caller matching remains available during migration.

Still required:

- replace and revoke all legacy keys that rely on caller-selected routes;
- implement signed dynamic metadata with replay bounds;
- preserve metadata trust classes in request analytics and audit fixed-value
  configuration changes;
- restrict future metadata-derived policy subjects to trusted values.

Closure criteria:

- every route-affecting metadata value is server-derived, fixed to the key, or
  cryptographically verified;
- untrusted correlation metadata cannot affect route or policy selection;
- existing integrations have an explicit migration path and rollback behavior.

## Streaming lifecycle and concurrency settlement

Status: regression fixed in code on 2026-07-22; redeployment and production verification required

### Impact

Dayboard requests failed first with `Connection error` and later with a friendly
"AI service is busy" message. The latter was a Northgate-generated
`429 CONCURRENCY_LIMIT_EXCEEDED`, not evidence that the upstream model provider
was busy. Successful streamed provider calls could leave active concurrency
leases behind long enough for a later call in the same agent run to be rejected.

### Incident sequence

1. Northgate continued reading after the provider's SSE `[DONE]` event while
   waiting for upstream EOF. A provider connection that stayed open therefore
   kept request finalization and its concurrency lease alive. Commit `703e8b8`
   made the terminal SSE event end the upstream stream.
2. Rebuilding only the base Compose file omitted the platform override and
   detached Northgate from the shared `platform-infra` network. Dayboard could
   no longer resolve the gateway. The host now selects both Compose files, and
   commit `91723b2` documents and preserves the platform topology during upgrades.
3. Ending the stream at `[DONE]` exposed a second cancellation path. Starlette's
   response task cancelled its disconnect listener, and AnyIO cancellation
   propagated into Northgate's finalization work. `asyncio.shield` did not protect
   a child task that inherited the cancelled AnyIO scope. PostgreSQL request
   records remained `started` and Redis leases were released only after later
   task cleanup. Commit `bdb19e2` uses an AnyIO shielded cancellation scope and
   adds a regression test with suspended settlement operations.
4. A later Dayboard Run exposed an uncovered exhausted-retry path. Two model
   rounds succeeded, then the next Northgate request received two upstream `502`
   responses. The first provider attempt settled as `retryable_status`, but the
   final attempt was returned through `StreamingResponse`; its provider attempt
   and request remained `started`, and its Redis concurrency lease remained
   active. Two immediate OpenAI SDK retries then received Northgate
   `429 CONCURRENCY_LIMIT_EXCEEDED`. The Dayboard Run failed after 31 seconds with
   17,621 accounted tokens. Relevant IDs are Dayboard Run
   `7f6cf42f-f850-45c0-b6b2-2c07ad3aa539`, Northgate request
   `req_d7d6805e4e5c4292bff994b52e12e90d`, and final rejected request
   `req_2ac7cce658874830a34b7007e0097a08`.

These were coupled lifecycle and deployment defects, not three independent
provider outages.

### Root-cause classification

The upstream provider's connection behavior exposed the defect, but provider
instability was not the primary cause.

- The direct technical cause was coupling stream termination and downstream
  cancellation to PostgreSQL record settlement, Redis lease release, and the
  application task lifecycle. The deployment path also allowed a valid base
  Compose rebuild to omit required application-network connectivity.
- The verification cause was testing SSE, concurrency, storage, and deployment
  separately without exercising the production combination: real PostgreSQL and
  Redis, sequential model/tool/model calls, terminal-event cancellation, and
  container replacement.
- The rollout cause was placing Northgate on an important M4 production path
  before that combined path had passed a soak test and before reconciliation
  existed for partial settlement.
- The incident-management cause was initially treating restored service as
  closure. Recovery established mitigation only; prevention, detection,
  reconciliation, and soak-test criteria remain open below.

### Why existing verification missed it

- Streaming tests covered incremental delivery, terminal events, and disconnects,
  but mocked settlement methods completed without yielding. They did not reproduce
  cancellation during real PostgreSQL and Redis I/O.
- Policy tests proved atomic admission under parallel requests, but did not run a
  sequence of model/tool/model calls under a low concurrency limit while injecting
  terminal-event cancellation.
- The platform network was an optional Compose override selected by an operator
  command, so a valid base Compose rebuild could produce an invalid application
  topology.
- Health checks proved each process was alive. They did not prove that an
  application container could resolve and call its configured gateway.

### Required hardening

Implemented on 2026-07-22:

- A real PostgreSQL and Redis integration test now runs three sequential SSE
  requests under concurrency limit one. Each stream terminates at `[DONE]`, each
  request and attempt reaches `succeeded`, and the lease count returns to zero
  before the next request.
- `northgate-reconcile` now previews stale records and expired leases by default.
  Explicit `--apply` releases recoverable leases and marks stale request and
  attempt records `settlement_incomplete` without inventing usage. Its integration
  test verifies preview, application, and idempotent repetition.
- `northgate_settlement_failures_total{stage}` now exposes request, attempt, and
  policy settlement failures with bounded labels.
- `/metrics` now exports the count and oldest age of `started` request records and
  active concurrency leases. Admission stores lease start metadata separately
  from its renewable expiry, and settlement/reconciliation remove both atomically
  within each Redis operation.
- Stream finalization now shields upstream close, cache write, route health,
  attempt settlement, request settlement, and policy settlement as one ordered
  operation. Parameterized cancellation tests suspend each boundary independently
  and verify that every later stage still completes.
- Regression coverage distinguishes a Northgate
  `CONCURRENCY_LIMIT_EXCEEDED` envelope and gateway-error metric from a provider's
  native `429` response and provider-attempt metric.
- `scripts/validate-compose-topology.sh` is now a mandatory preflight of the
  supported upgrade command. It fails before build, backup, migration, or
  replacement when the merged service lacks either required network or the
  configured external platform network does not exist.
- The supported upgrade command optionally executes a readiness request from the
  configured application container, and deployable Prometheus rules cover stale
  requests, stale leases, settlement failures, metrics collection failures, and
  Northgate concurrency rejections.
- The isolated Compose soak harness now exercises non-streaming calls, complete
  SSE, two-step tool calls, deliberate client disconnects, injected primary
  failures with fallback, and a Northgate container recreation. Its 2026-07-22
  verification completed 10 iterations with 12 fallback attempts and ended with
  zero `started` records and zero active leases.
- Migration `0012` adds a durable settlement outbox. `northgate-worker` claims
  events with `FOR UPDATE SKIP LOCKED`, records database and policy progress
  separately, and retries idempotently. A real PostgreSQL/Redis test injects a
  policy failure after database settlement and verifies recovery without double
  counting tokens.
- `/metrics` exposes pending outbox count, oldest pending age, and exhausted
  events. The deployable Prometheus rules alert on delayed or failed events.
- `NORTHGATE_SETTLEMENT_OUTBOX_ENABLED` moves streamed provider-response,
  cache-hit, and final provider-unavailable/timeout accounting to the outbox.
  Northgate immediately attempts the event once; a later failure is retried by
  `northgate-worker`, while an enqueue failure falls back to inline settlement
  and increments the `outbox_enqueue` failure metric.
- Revision `0013` allows multiple idempotent events per request. Timeout,
  transport-error, and retryable-status attempts now use durable
  `attempt:{attempt_id}` events independently of the terminal request event.
- Outbox-enabled readiness now requires at least one active worker heartbeat.
  The isolated failure soak verified worker loss becomes unready after the TTL
  and that a Redis settlement retry recovers across Northgate recreation.
- `northgate_settlement_worker_available` and the deployable alert rule expose
  worker heartbeat loss independently of HTTP readiness.
- Exhausted retryable provider `5xx` responses are now drained and settled before
  Northgate returns its stable `PROVIDER_UNAVAILABLE` response. Final provider
  `429` responses remain provider-native and distinguishable from Northgate
  policy rejection.
- A concurrency-limit-one regression test sends two consecutive requests, each
  receiving two upstream `502` responses. It verifies that request and attempt
  records leave `started` and the Redis lease is released before the second
  request, both for inline settlement and when the first outbox process fails.
- The supported Compose upgrade command now requires
  `NORTHGATE_APPLICATION_PROBE_CONTAINER` before any build, backup, migration, or
  replacement and unconditionally runs the application-side readiness probe
  after Northgate becomes ready.
- The settlement worker now has a heartbeat-backed container healthcheck, and the
  outbox-aware topology preflight rejects worker services without one. Compose
  upgrade `--wait` therefore includes worker availability.
- SQLAlchemy pool invalidations now increment
  `northgate_database_connection_invalidations_total{reason}`. Explicit
  `asyncio.CancelledError` invalidations use the bounded `cancelled` reason and
  have a deployable Prometheus alert.

Still required:

- Connect worker heartbeat alerts to the production deployment controller.
- Connect the provided alert rules to the production Alertmanager.

### Closure criteria

This issue can be closed only when all of the following are demonstrated:

- No active concurrency lease or `started` request record remains after the
  sequential streaming integration suite, including injected cancellation cases.
- A failed settlement is observable and recoverable without restarting Northgate
  or manually editing Redis.
- The supported upgrade command preserves application connectivity by construction
  and fails before replacement when the required network topology is absent.
- A production-like soak completes without false
  `CONCURRENCY_LIMIT_EXCEEDED`, leaked database connections, or unexplained
  request records.
