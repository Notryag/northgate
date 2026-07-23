# Roadmap

Status: proposed  
Last reviewed: 2026-07-22

Milestones are ordered by dependency. A milestone is complete only when its
exit criteria are verified; feature count alone is not completion.

The accepted 2026-07-22 architecture assessment and ordered refactoring slices
are maintained in [Architecture review and refactoring priorities](architecture-review.md).
Settlement recovery, pipeline decomposition, metadata trust, and configuration
snapshots take precedence over adding provider or endpoint breadth.

## M0: Foundation

Status: complete (2026-07-16)

Deliverables:

- Accepted product scope and architecture.
- Language and framework decision. Accepted in
  [ADR 0001](decisions/0001-python-service-stack.md).
- Repository tooling, CI, migrations, and local Compose environment.
- Request ID, structured logging, configuration, and secret-handling baseline.

Exit criteria:

- A contributor can run checks and an empty service locally from documented commands.
- No production provider credential is required for the test suite.

## M1: Transparent proxy

Status: complete (2026-07-16)

Implemented so far:

- OpenAI-compatible chat-completions forwarding for streaming and non-streaming responses.
- Configuration-backed and PostgreSQL-backed application authentication and routing.
- Encrypted provider credential storage and an idempotent bootstrap command.
- Durable request records with status, latency, first-token latency, and reported tokens.

Verification:

- A Dayboard agent completed a two-call tool-execution loop through the database-backed gateway.
- Gated-stream verification confirms the first SSE chunk is sent before the upstream completes.
- Client application credentials and request content are absent from upstream headers and logs.

Deliverables:

- OpenAI-compatible chat-completions endpoint.
- Streaming and non-streaming forwarding.
- Project, gateway, application key, and encrypted provider credential storage.
- Stable gateway errors and provider timeout behavior.
- Durable request records with latency, status, model, and reported tokens.

Exit criteria:

- Dayboard can complete a representative tool-calling run through Northgate.
- Streaming chunks arrive without whole-response buffering.
- Credentials and content do not appear in logs or errors.

## M2: Limits and analytics

Status: complete (2026-07-16)

Implemented so far:

- Atomic Redis admission for per-minute requests, active concurrency, and daily tokens.
- Expiring per-request concurrency leases with renewal during long streams.
- Token reservation before forwarding and exactly-once adjustment to reported usage.
- Versioned per-model pricing with atomic daily and monthly spend reservations.
- Operator-authenticated usage summary and hourly/daily time-series APIs.
- React operator dashboard for requests, tokens, cost, latency, errors, and usage buckets.

Verification:

- Parallel requests cannot exceed configured request or concurrency admission bounds.
- Token and spend reservations settle exactly once to provider-reported usage.
- Analytics summary totals reconcile with the underlying request records.
- Token admission is componentized and observable: model-aware prompt estimates,
  explicit unknown-model fallback, request/route/model/global output defaults,
  retry/fallback multiplier, margin, released tokens, and aggregate calibration
  findings are shared by REST, CLI, MCP, metrics, and Console.

Deliverables:

- Redis-backed request, token, and concurrency limits.
- Daily and monthly spend limits using versioned model pricing.
- Usage summary and time-series APIs.
- Operator dashboard for traffic, tokens, cost, latency, and errors.
- Exactly-once usage settlement by Northgate request ID.

Exit criteria:

- Parallel tests demonstrate that configured limits cannot be oversubscribed beyond documented bounds.
- Analytics totals reconcile with immutable request records.

## M3: Routing and reliability

Status: complete (2026-07-17)

Implemented so far:

- Multiple ordered OpenAI-compatible routes per gateway.
- Bounded per-route retries and fallback on configured status codes or transport failures.
- Durable provider-attempt records for status, latency, usage, cost, and ambiguous outcomes.
- Operator API for inspecting every attempt behind a Northgate request ID.
- Redis-backed route circuit breakers with single-request half-open probes and explicit recovery windows.
- Deterministic weighted routing and exact-match metadata rules with ordered fallback.
- Redis-backed exact request caching with bounded entries and cache-hit accounting.
- Explicit OpenAI-compatible and Azure OpenAI provider adapters with isolated authentication.

Verification:

- Failure injection confirms ordered fallback, open-circuit route skipping, and recovery behavior.
- Provider attempts remain individually visible with usage, cost, latency, and ambiguous outcomes.
- Exact-cache tests confirm hits avoid provider calls while oversized and variant requests bypass.
- Azure adapter tests confirm deployment URL encoding, API version forwarding, and credential isolation.

Deliverables:

- Multiple provider adapters.
- Ordered fallback and bounded retry policies.
- Weighted and metadata-based routes.
- Health-aware routing with explicit recovery behavior.
- Configurable semantic or exact request caching.

Exit criteria:

- Failure-injection tests verify routing and retry behavior without duplicate billing attempts hidden from operators.

## M4: Existing-system adoption

Status: in progress

The streaming lifecycle and deployment-topology incident remains open for
hardening even though its immediate production failures are mitigated. See
[Known issues and hardening work](known-issues.md) for the required integration,
reconciliation, observability, and soak-test exit criteria.

### Settlement recovery hardening

Accepted on 2026-07-22 after review of the first durable-settlement implementation.
Work is ordered by correctness risk:

1. **Complete.** Make real PostgreSQL and Redis integration tests a required CI job. The job
   must apply migrations, select tests marked `integration`, and fail rather than
   silently skip when its stores are unavailable.
2. **Complete.** Prevent `northgate-reconcile` from changing request or attempt records while
   a `pending`, `retry`, or `processing` settlement event can still recover them.
   The final apply query must repeat this guard rather than relying only on an
   earlier candidate read.
3. **Complete.** Treat zero-row settlement updates explicitly. They are successful only when
   the existing record already matches the event payload's terminal fields;
   missing or conflicting records must keep the event recoverable and visible.
4. **Complete.** Cover the reconciliation/outbox crossing path: a delayed event protects its
   old `started` records, then completes with the exact usage and outcome after
   it becomes available.
5. **Complete.** Revisit worker readiness separately. A missing heartbeat with no overdue
   backlog should be degraded rather than necessarily removing the data plane
   from service; an overdue recoverable backlog remains a readiness failure.
6. **Complete.** Add settlement payload versioning, pending-worker query indexes, and a bounded
   retention/archive policy after the correctness work is complete.

All six tasks were completed and verified on 2026-07-22. The retention command is
deliberately scheduler-neutral and operates in bounded batches.

Implemented so far:

- Operator-authenticated control APIs for organizations, projects, gateways,
  application keys, encrypted provider credentials, and routes.
- React management workspaces for organizations, projects, application-key
  issuance/revocation, encrypted provider credential creation/rotation, route
  references, and read-only readiness/stale-settlement operations.
- One-time application key issuance, key revocation, provider secret rotation,
  and route traffic controls required for gradual cutover and rollback.
- Route/provider attempt distribution API and React console visibility for
  weighted traffic, retries, fallback load, tokens, cost, and latency.
- Gateway policy control API for request, concurrency, token, spend, and exact-cache limits.
- Real PostgreSQL/Redis sequential streaming settlement verification and an
  idempotent `northgate-reconcile` recovery command for stale ambiguous records.
- Durable settlement migration `0012`, an idempotent coordinator, and the
  `northgate-worker` entry point. A real-store test verifies recovery when
  PostgreSQL terminal records commit but the first Redis settlement fails.
- Guarded outbox handoff for streamed responses, cache hits, and final provider
  failures, with inline fallback when event enqueue is unavailable.
- Multi-event settlement keys in migration `0013`, with durable intermediate
  timeout, transport-error, and retryable-status attempt settlement.
- First request-pipeline extraction: bounded body, metadata, model parsing, token
  estimation, and forwarded request headers now live in `proxy_input.py`.
- Route-planning extraction: application-key route resolution, metadata-based
  candidate selection, and primary adapter validation now live in
  `route_planning.py`.
- Provider-attempt execution extraction: adapter request construction, streaming
  send, timeout/connect/ambiguous transport classification, and retryable-response
  consumption now live in `attempt_execution.py`. Focused contracts preserve final
  provider `429` passthrough and exhaust final retryable `5xx` responses.
- Stream-relay extraction: upstream byte forwarding, cache-body bounds, SSE terminal
  detection, cancellation/transport outcome classification, and shielded finalizer
  handoff now live in `stream_relay.py`.
- Settlement-stage extraction: request/attempt guarded outbox helpers and aggregate
  attempt totals now live in `proxy_settlement.py`; streamed cache, route-health,
  ledger, and policy finalization now lives in `stream_finalization.py`.
- Metadata binding migration `0014`: trusted application keys route only on
  server-derived project/application identity and operator-configured fixed
  values. Existing keys retain explicit legacy matching for staged replacement.
- Metadata trust ledger migration `0015`: request records preserve `server`,
  `fixed`, `untrusted`, and `legacy` classes; tenant aggregation excludes
  caller-controlled values while diagnostics expose their class.
- Bounded settlement failure metrics for request, attempt, and policy stages.
- Full stream-finalization cancellation shielding with independently suspended
  close, cache, route-health, attempt, request, and policy boundaries.
- Isolated container-recreation soak coverage for streaming, tool calls, client
  disconnects, injected fallback, and direct PostgreSQL/Redis reconciliation.
- Environment-driven compatibility verifier and a canary, reconciliation, and rollback guide.
- Compose platform-network override that keeps Northgate data stores isolated while
  exposing the gateway to existing application containers.

Verification:

- On 2026-07-17, an isolated PostgreSQL database was configured entirely through
  the control API, then used for a real OpenAI-compatible request through the
  database route to the mock provider.
- Operator authentication rejected an invalid key; application and provider
  credential lists exposed no secret or digest material; encrypted storage was
  checked for plaintext absence.
- Disabling the route stopped traffic with `503`, re-enabling restored the route,
  provider secret rotation completed without disclosure, and revoking the
  application key rejected subsequent traffic with `401`.
- Route distribution was checked against existing attempt records: one failed
  primary attempt and one successful fallback attempt appeared as separate 50%
  shares, with tokens and cost attributed only to the reporting fallback.
- Policy creation and replacement were exercised against an isolated database:
  create returned `201`, replacement returned `200`, explicit `null` disabled
  limits, invalid zero values returned `422`, and an unknown gateway returned `404`.
- The compatibility command passed non-streaming, SSE, and tool-call checks through
  a real local Northgate process. The first SSE event arrived in 18 ms while the
  mock stream remained open for about 5 seconds, confirming incremental delivery.
- The Dayboard-pinned LangChain/OpenAI client sent trusted tenant, user, and run
  metadata through a real Northgate process to the mock provider. North `10d2280`
  and Dayboard `3429776` contain the default-off, rollback-safe integration path;
  no production traffic migration is claimed yet.
- Dayboard `b6c0f58` and North `63ff252` add tenant allowlist canary selection:
  selected tenants use a separate Northgate connection while all unmatched
  tenants retain the original provider configuration.

Deliverables:

- Configure a new application without direct database access or bootstrap-only environment variables.
- Document base URL replacement, application metadata, canary traffic, and rollback.
- Validate non-streaming, streaming, tool calls, timeout, retry, and fallback compatibility.
- Reconcile provider attempts, tokens, and cost during a real application canary period.

Exit criteria:

- An existing OpenAI-compatible application can move a bounded traffic share to
  Northgate and return to its prior provider path without a code change.
- Operators can explain traffic distribution and every provider attempt from
  Northgate records and metrics.

## M5: Open-source operations

Status: in progress

Implemented so far:

- Authenticated Prometheus exposition with bounded HTTP, provider, cache, route, token, and cost labels.
- OTLP/HTTP server traces with W3C propagation and content-free gateway lifecycle events.
- Accepted forward-only production migration and stable environment-configuration policies.
- Verified Compose backup, destructive restore, and maintenance-window upgrade workflows.

Deliverables:

- Stable configuration and migration policy.
- A production deployment path appropriate to demonstrated adoption needs.
- Backup, restore, upgrade, and incident guides.
- OpenTelemetry and Prometheus export.
- Security policy, contribution guide, and selected open-source license.

Exit criteria:

- A clean environment can deploy, upgrade, back up, and restore Northgate using only public documentation.

Kubernetes and Helm packaging are intentionally not current priorities. They can
be selected later if real deployments require them.

## Deferred

Guardrails, DLP, prompt evaluation, advanced semantic caching, and visual route
builders are intentionally deferred until the proxy, accounting, and policy
contracts are stable.
