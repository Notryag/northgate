# Architecture review and refactoring priorities

Status: accepted assessment; implementation items are tracked individually  
Last reviewed: 2026-07-22

Northgate has the right product boundary and a strong request/attempt accounting
model, but the data-plane implementation has reached the point where reliability
work and decomposition should precede new protocol and provider breadth. It is a
production prototype with demonstrated adoption, not yet a broadly extensible AI
gateway platform.

This assessment intentionally does not use provider count or feature count as the
primary benchmark. Northgate's intended differentiator is explainable,
settleable, and recoverable AI traffic for agent applications.

## Decision summary

| Proposal | Decision | Reason and next boundary |
| --- | --- | --- |
| Durable settlement coordinator and reconciliation worker | Implemented; rollout pending | Revisions `0012`/`0013`, multi-event idempotent PostgreSQL/Redis settlement, `SKIP LOCKED` worker, recovery tests, metrics, and guarded proxy handoff are implemented. Production profiles must still deploy and monitor the worker before enabling it. |
| Decompose `proxy_chat_completions` into a request pipeline | Adopt incrementally | The function owns too many lifecycle stages. Extract typed context and stage services while preserving one endpoint and its existing behavior. Do not combine this with a protocol expansion. |
| Bind or sign routing metadata values | Adopt before adding more metadata routes | An allowed key is not proof that its caller-supplied value is trustworthy. Attribution metadata and route-affecting metadata need separate trust rules. |
| Real PostgreSQL, Redis, streaming, and cancellation integration tests | Adopt first | This directly covers the incident class that unit stubs missed and is an exit criterion for current production hardening. |
| Versioned in-memory gateway configuration snapshots | Adopt after settlement hardening | It removes repeated database reads and credential decryption from the hot path, but requires explicit invalidation, stale-config behavior, and secret-rotation tests. PostgreSQL remains authoritative. |
| Separate data-plane, control-plane, and worker entry points | Adopt after module boundaries exist | Independent entry points are useful before separate services are required. Deployments may continue to run them together until load or availability evidence justifies isolation. |
| Organization/project/gateway/application/tenant/user policy subjects | Adopt in stages | Application-key and trusted-tenant subjects solve the immediate noisy-neighbor problem. A generic hierarchy should follow only after settlement supports multiple atomic reservations. |
| Request body size limit | Implemented | Requests are capped by `NORTHGATE_MAX_REQUEST_BODY_BYTES`, including chunked bodies, before complete buffering. |
| Validate fallback routes lazily | Implemented | The selected primary is validated before admission; an invalid fallback is isolated when reached and cannot reject a successful primary request. Control-plane validation remains desirable. |
| Remove Redis as an unconditional database-routing dependency | Do not adopt yet | Database routes can dynamically enable policy, cache, and circuit-breaker behavior. Without a configuration snapshot, omitting Redis at startup would make those accepted route contracts unavailable or silently ineffective. Revisit with snapshot capability. |
| Provider-specific HTTP connection pools | Defer pending evidence | A single bounded client is simpler today. Add pool isolation when metrics or load tests show one provider can starve unrelated routes. |
| Incremental retry/fallback budget reservations | Adopt after settlement coordinator | It improves admission accuracy, but changes cost-exposure semantics and requires atomic multi-step reservation plus crash recovery. |
| Responses, Messages, embedding, image, and audio endpoints | Defer | Protocol breadth would multiply the current lifecycle and settlement complexity. Add it only after the pipeline and settlement contracts are reusable. |

## Target data-plane pipeline

The endpoint should become an orchestrator over explicit stages:

```text
RequestContext
  -> authentication and trusted identity
  -> configuration snapshot
  -> bounded body and metadata validation
  -> policy admission
  -> route plan
  -> attempt execution and stream relay
  -> durable settlement handoff
```

`RequestContext` should carry immutable request identity, trusted policy subjects,
the selected configuration version, parsed request facts, and accounting handles.
It must not contain an unbounded request or response body. Attempt execution owns
provider-specific validation and transport. Settlement owns all terminal record
and reservation transitions.

## Settlement direction

The target is not to move all accounting blindly out of band. Before response
headers, admission and request/attempt creation remain synchronous so Northgate
does not forward unaccounted traffic. At a terminal response or stream event, the
data plane writes an idempotent durable settlement event. A worker applies the
request record, attempt record, route health, cache, and Redis reservation steps,
recording progress so each can be retried.

The first implemented slice persists request/attempt terminal values and Redis
reservation settlement progress. Cache and route-health updates remain inline and
best-effort because they are reconstructible operational state. When explicitly
enabled, the provider-response stream path enqueues the event and immediately
attempts it once; failure remains durable for `northgate-worker`. An enqueue
failure is observable and falls back to inline settlement. Cache hits and final
provider-unavailable/timeout outcomes now use the same guarded handoff.
Revision `0013` adds multiple idempotent event keys per request. Timeout,
transport-error, and retryable-status attempts use `attempt:{attempt_id}` events,
while aggregate request settlement uses `terminal`. Inline settlement remains
only as an observable fallback when durable event enqueue itself is unavailable.

Unknown usage remains unknown. Reconciliation may release expired concurrency
capacity and mark an outcome ambiguous, but must not invent token or cost values.

## Metadata trust model

Until value binding is implemented, caller-provided metadata is suitable for
correlation and attribution only. Operators must not use it to select privileged
models, higher budgets, regulated data regions, production environments, or any
other authorization-sensitive route.

The intended model separates:

- server-derived identity from the application key, such as project and application;
- key-bound fixed dimensions configured by an operator, such as environment;
- signed dynamic dimensions supplied by a trusted application, such as tenant;
- untrusted correlation dimensions, such as a run ID.

Route matching and policy subjects may consume only server-derived, fixed, or
verified signed dimensions. Analytics may retain explicitly allowed untrusted
dimensions with their trust class visible.

## Ordered implementation slices

1. Completed: real-infrastructure streaming settlement tests and the durable
   settlement event state machine.
2. Completed in code: PostgreSQL outbox, multi-event idempotent settlement coordinator, worker
   entry point, reconciliation command, guarded stream handoff, backlog metrics,
   and alerts are implemented. Require the worker profile in production
   deployments that enable the handoff.
3. In progress: bounded body/metadata/model/header parsing lives in
   `proxy_input.py`; application-key route resolution, metadata selection, and
   primary validation live in `route_planning.py`; provider request construction,
   streaming send, transport-failure classification, and retryable-response
   consumption live in `attempt_execution.py`. Its terminal result preserves final
   provider `429` passthrough and exhausts final retryable `5xx` responses. Byte
   relay, SSE terminal detection, cancellation outcome classification, and shielded
   finalizer handoff live in `stream_relay.py`. Cache, route-health, attempt,
   request, and policy terminal settlement now live in `stream_finalization.py`,
   using shared guarded handoff helpers from `proxy_settlement.py`.
4. Add metadata trust classes and value binding, then migrate route matching to
   trusted metadata only.
5. Compile and atomically swap versioned gateway snapshots; define stale-snapshot
   and credential-rotation behavior.
6. Add independent data-plane and control-plane entry points and the first
   application-level policy subject.

New provider protocols are intentionally sequenced after these slices.
