# Roadmap

Status: proposed  
Last reviewed: 2026-07-15

Milestones are ordered by dependency. A milestone is complete only when its
exit criteria are verified; feature count alone is not completion.

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

## M4: Open-source operations

Deliverables:

- Stable configuration and migration policy.
- Helm chart or equivalent production deployment path.
- Backup, restore, upgrade, and incident guides.
- OpenTelemetry and Prometheus export.
- Security policy, contribution guide, and selected open-source license.

Exit criteria:

- A clean environment can deploy, upgrade, back up, and restore Northgate using only public documentation.

## Deferred

Guardrails, DLP, prompt evaluation, advanced semantic caching, and visual route
builders are intentionally deferred until the proxy, accounting, and policy
contracts are stable.
