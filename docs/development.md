# Development workflow

Status: accepted  
Last reviewed: 2026-07-22

The verification cadence below is normative. The production verification log is
append-oriented historical evidence and may mention earlier migration heads,
test counts, readiness behavior, or deployed revisions. Use
[Current implementation state](current-state.md) for present behavior.

## Verification cadence

Development should keep the main implementation flow moving and use concentrated
verification instead of rerunning the complete test suite after every edit.

- During a feature slice, run only checks needed to resolve a concrete risk or failure.
- Run the relevant focused checks when a contract, migration, security boundary, or
  streaming behavior changes.
- Run the complete backend and frontend quality checks once at a meaningful commit,
  merge, milestone, or release boundary.
- Record manual integration or recovery verification in the owning roadmap or
  operations page when it establishes an exit criterion.
- CI remains the independent full-suite gate for every push and pull request.

This cadence changes when tests run, not the quality bar. A feature slice is not
complete while a known failure remains or its critical path has not been exercised.

## Production verification log

### 2026-07-22: Isolated settlement and container-recreation soak

- Added `docker-compose.soak.yml` and `scripts/run-compose-soak.sh`. They use the
  isolated `northgate-soak` Compose project, private network, database volume, and
  host port, and remove all of those resources on exit.
- Ran two five-iteration phases. Every iteration included a non-streaming call, a
  complete SSE call, a two-request tool execution loop, and a stream closed by the
  client after its first event.
- Configured the primary mock provider to return `503` every fourth call and kept
  a healthy fallback. The attempt ledger recorded 12 fallback attempts.
- Force-recreated only the isolated Northgate container between phases and waited
  for readiness before resuming traffic.
- The final direct storage checks found zero request records in `started` and zero
  active Redis concurrency leases. The harness exited successfully and removed
  its containers, network, and database volume.
- Ran the new application-side readiness probe from the existing
  `dayboard-api-1` container to `http://northgate:8080/health/ready`; it returned
  the expected ready response without changing or recreating production containers.

Run the same deterministic local acceptance with:

```sh
./scripts/run-compose-soak.sh
```

### 2026-07-22: Guarded durable settlement handoff

- Applied migrations `0012` and `0013` and enabled the outbox only in the real-storage
  streaming integration test. Three sequential SSE requests each produced a
  completed durable event, terminal request and attempt records, and zero active
  concurrency leases.
- Injected a Redis policy-settlement failure after PostgreSQL terminal updates.
  The event retained its database progress, retried idempotently, released the
  lease, and settled five tokens without double counting.
- Verified an outbox enqueue failure falls back to inline request/attempt
  settlement and increments the bounded `outbox_enqueue` failure metric.
- Ran the real PostgreSQL/Redis tests with `PYTHONWARNINGS=error`; all three passed
  without leaked SQLAlchemy connections. The complete backend suite passed 78
  tests, both Compose configurations validated, and Alembic reported `0013` as
  the single head.
- Ran `northgate-worker --once` against the migrated local PostgreSQL and Redis;
  it drained available work and exited successfully.
- Verified one request can own independent `attempt:{attempt_id}` and `terminal`
  events. Retryable status, timeout, transport error, cache hit, route-health
  failure, and attempt-ledger failure exits all use the guarded durable handoff.
- Extended the isolated soak with worker heartbeat/readiness and failure recovery.
  The current harness expects degraded readiness after stopping the worker and
  reserves `503` for an overdue recoverable backlog. Stopping Redis after an active
  lease produced a retry event; Redis, Northgate, and the worker were restored,
  and final reconciliation reported `started=0`, `leases=0`, `pending=0`,
  `failed=0`, with 12 fallback attempts. Dedicated Compose resources were removed.
- Added worker-aware Compose upgrade validation: when outbox is enabled, the
  supported upgrade builds, stops, and starts Northgate and the settlement worker
  together and rejects a merged configuration without the worker service.
- Fixed the exhausted-retry regression: a final retryable provider `5xx` is
  drained and settled before Northgate returns `PROVIDER_UNAVAILABLE`. The
  concurrency-limit-one regression sends two requests through two `502` attempts
  each and proves the second request is admitted immediately in inline and
  first-process-failure outbox modes. Provider-native final `429` remains intact.
- Made the application-container readiness probe mandatory in the supported
  Compose upgrade command. A missing probe container now stops the command before
  build, backup, migration, or service replacement.
- Added `northgate-worker --healthcheck` and attached it to the Compose worker
  service. It checks the Redis heartbeat used by data-plane readiness, while the
  topology preflight now rejects an outbox worker without a healthcheck.
- Instrumented SQLAlchemy pool invalidation events with bounded cancellation,
  error, and unspecified reasons, and added an alert for explicit cancelled
  database connections.
- Extracted provider request construction and streaming transport execution into
  `attempt_execution.py`. Focused tests cover successful response handoff and
  timeout, connection, and ambiguous transport failures without changing retry or
  settlement ownership.
- Moved retryable-response consumption behind the same attempt boundary. Focused
  contracts verify intermediate provider `429` is consumed for retry, final `429`
  is passed through unchanged, and an exhausted final `502` is drained with usage
  before request settlement.
- Extracted stream byte relay and its shielded finalizer handoff into
  `stream_relay.py`. Focused tests prove SSE `[DONE]` prevents reads past the
  terminal event and interrupted upstream reads report a transport failure before
  settlement finalization.
- Extracted shared request/attempt settlement helpers into `proxy_settlement.py`
  and moved streamed cache, route-health, ledger, outbox, and policy finalization
  into `stream_finalization.py`. The endpoint module now orchestrates those stages
  without implementing the stream lifecycle itself.
- Added migration `0014` and trusted application-key route metadata. Existing keys
  are backfilled to explicit legacy mode for staged replacement; new trusted keys
  use only server identity and fixed operator values, with negative tests against
  caller tenant spoofing.
- Added migration `0015` and per-key request metadata trust classes. Usage records
  retain server, fixed, untrusted, and legacy provenance; tenant aggregation only
  consumes trusted fixed or future signed values.
- Began the request-pipeline decomposition by extracting bounded request input,
  metadata/model parsing, token estimation, and allowed forwarded headers into
  immutable `ProxyRequestInput`; the existing proxy behavior suite remained green.
- Extracted application-key route resolution, metadata candidate selection, and
  primary adapter validation into `route_planning.py`. Fallback validation and
  provider HTTP execution deliberately remain together for the next executor slice.

### 2026-07-17: Dayboard single-tenant canary

- Built the Dayboard API/worker image from clean commit `b6c0f58`; uncommitted
  workspace changes were excluded from the image.
- Deployed the image to API and worker while leaving the web service and the
  original provider connection unchanged.
- Enabled Northgate only for the newer of two production tenants. Each tenant had
  one member at the time of deployment, so the allowlist represented one user.
- Confirmed both containers were healthy, could resolve `northgate` on the shared
  `platform-infra` network, and received HTTP 200 from Northgate readiness.
- Exercised Dayboard's deployed selection logic without another model request:
  the canary tenant selected the Northgate connection and trusted metadata, while
  the control tenant selected the original connection without Northgate metadata.
- Confirmed the Northgate and Dayboard startup logs contained no errors after the
  deployment.

The pre-canary Dayboard environment backup is stored at
`/var/backups/dayboard/config/dayboard-env-pre-canary-20260717T151852Z.env` with a
root-only checksum. Rollback restores that file and recreates only the Dayboard API
and worker containers. Provider and application secrets are stored separately as
root-only files and are intentionally not recorded here.

### 2026-07-18: Dayboard full tenant rollout

- Observed the single-tenant deployment for 11 hours. Northgate, Dayboard API, and
  Dayboard worker remained healthy, and the previous Northgate documentation CI
  run completed successfully.
- No organic canary traffic arrived during that window, so ran one minimal
  Dayboard agent smoke through the deployed factory, Northgate, and the real
  provider without writing Dayboard business data or printing model content.
- The smoke completed with HTTP 200 and outcome `succeeded`: one provider attempt,
  no retry or fallback, 1,169 ms total latency, 1,164 ms first-token latency, and
  complete tenant, user, and run attribution.
- Added the remaining production tenant to the allowlist. Production contained two
  tenants with one member each at rollout time; both now select Northgate and
  trusted metadata through Dayboard's deployed configuration.
- Recreated only the API and worker containers, confirmed both healthy, confirmed
  HTTP 200 from Northgate readiness, and found no startup or proxy errors.
- Kept Dayboard's original provider connection in the environment for rollback.

The pre-full-rollout environment backup is stored at
`/var/backups/dayboard/config/dayboard-env-pre-full-northgate-20260718T023133Z.env`
with a verified root-only checksum. Restoring it and recreating only API and worker
returns Dayboard to the single-tenant canary.

### 2026-07-18: Tenant usage analytics

- Added operator-only tenant aggregates and the React console tenant table in
  commit `7fc90cb`.
- Ran Ruff lint and format checks for the affected backend files and the focused
  tenant analytics test; the test passed, including operator authorization and
  omission of user/run metadata.
- Ran the console TypeScript check and production build successfully. No browser
  screenshots were created.
- Executed the SQLAlchemy tenant aggregation against the production PostgreSQL
  schema before deployment; all 19 historical records were classified as
  succeeded with no in-flight or error records.
- Rebuilt and replaced only the Northgate application container. The deployed
  endpoint rejected an unauthenticated request with HTTP 401 and returned, for its
  default 24-hour range, six successful requests across three groups, including two
  attributed tenant groups, with no errors or in-flight records.
- Confirmed the console returned HTTP 200, its bundle contained the tenant view,
  and Dayboard still received HTTP 200 from Northgate readiness over the shared
  platform network.

### 2026-07-18: Effective model price management

- Added append-only model price list/create control APIs and a React console price
  form in commit `d8427b1`. The form accepts dollars per one million tokens and the
  API stores integer micro-USD with a timezone-qualified effective timestamp.
- Ran Ruff lint and format checks, the focused control-price and integer-cost tests,
  the console TypeScript check, and the console production build successfully.
- Rebuilt and replaced only the Northgate application container. The deployed list
  endpoint rejected an unauthenticated request with HTTP 401 and returned the one
  existing `gpt-test` record to an operator with all price fields present.
- Confirmed a validation-only create request without a timezone returned HTTP 422
  and left the production table at one record.
- Confirmed the console and its pricing bundle were available, Dayboard still
  received HTTP 200 from Northgate readiness, and Northgate logged no deployment
  errors.
- Did not add a price for Dayboard's `gpt-5.4-mini`: its five recorded requests have
  no matched price, and an authoritative exact price could not be retrieved during
  this deployment. Operators must record the supplier or contract price before
  treating cost analytics as complete or enabling spend limits.

### 2026-07-22: Operator console phase 1

- Replaced the single-page dashboard composition with a React Router management
  shell and route-level lazy loading for Overview, Requests, Usage, and Pricing.
- Added TanStack Query server-state handling, Zod response validation, Ant Design
  management controls, and React Hook Form forms while retaining Recharts for the
  bounded traffic and token chart.
- Added a bounded newest-first mode to `GET /api/v1/usage/requests`, including
  `has_more` and known request cost, while retaining paired metadata correlation
  and redaction of metadata values and request content.
- Added request detail pages over the shared diagnostics API with findings,
  provider attempts, and redacted settlement progress. FastAPI now serves the SPA
  index for nested Console URLs.
- Ran the focused analytics and application tests, Ruff checks, Console TypeScript
  check, and production build. The Ant Design shared chunk remains larger than the
  default Vite warning threshold; business pages are separately lazy-loaded.

### 2026-07-22: Operator console Gateway management

- Added a Gateway workspace over the existing Operator control APIs with
  project/Gateway selection, Gateway creation, provider-credential-aware Route
  listing and creation, and explicit Route priority, weight, and enabled updates.
- Route creation exposes retry, circuit-breaker, and trusted metadata match fields
  without accepting credential secrets in the browser response.
- Added complete Gateway Policy replacement for request, concurrency, token,
  spend, and exact-cache limits. Blank fields deliberately map to disabled limits.
- Ran the Console TypeScript check and production build, then exercised the Web
  page against the real read-only production control responses. Both configured
  Gateways loaded with no browser or schema errors; no production mutation was
  performed.
