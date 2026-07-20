# Architecture

Status: accepted  
Last reviewed: 2026-07-15

## System boundary

```text
AI application
    |
    | application key + request metadata
    v
Northgate data plane
    |-- authentication
    |-- policy admission
    |-- route selection
    |-- provider adapter
    |-- usage settlement
    |
    v
AI provider

Northgate control plane ---> PostgreSQL
Northgate data plane    ---> Redis
Northgate data plane    ---> PostgreSQL usage writer
```

The data plane handles provider traffic. The control plane manages projects,
credentials, gateways, routes, policies, and analytics queries. They may begin
in one deployable service, but their modules and failure behavior must remain
separate so they can be scaled independently later.

## Proposed components

| Component | Responsibility | Durable state |
| --- | --- | --- |
| Proxy API | Authenticate, admit, route, stream, and normalize errors | None |
| Policy engine | Evaluate request, token, concurrency, and spend policies | Policy definitions in PostgreSQL; counters in Redis |
| Provider adapters | Preserve provider protocol and extract normalized usage | None |
| Usage writer | Settle request outcome, tokens, cost, timing, and route | PostgreSQL |
| Control API | Manage projects, keys, credentials, gateways, routes, and policies | PostgreSQL |
| Analytics API | Query pre-aggregated usage and operational health | PostgreSQL |
| Console | Operate configuration and inspect analytics | None |

## Request lifecycle

1. Assign or validate a stable Northgate request ID.
2. Authenticate the application key and resolve project and gateway scope.
3. Validate signed or key-bound request metadata.
4. Reserve request, concurrency, estimated token, and spend capacity.
5. Select a route from accepted configuration.
6. Forward the provider-native request and preserve streaming semantics.
7. Capture status, first-token latency, total latency, provider request ID, exact-cache result,
   provider-reported usage, and provider-reported cached prompt tokens.
8. Settle actual token and cost usage exactly once.
9. Release concurrency capacity and persist the terminal request record.
10. Emit metrics and traces without credentials or content by default.

## Storage ownership

PostgreSQL is authoritative for organizations, projects, gateways, keys,
encrypted provider credentials, routes, policies, pricing, audit events, and
settled usage. Redis contains rate-window counters, concurrency leases, short
cache entries, and other reconstructible state.

The request path must not synchronously run expensive analytics queries.
Usage writes should be append-oriented and idempotent by request ID.

## Availability behavior

- PostgreSQL unavailable before admission: reject requests that require policy or credential resolution.
- Redis unavailable: reject policy-controlled requests rather than bypassing limits.
- Analytics or console unavailable: proxy traffic continues when configuration is already available.
- Provider unavailable: apply only configured retries or fallback routes.
- Client disconnect: cancel upstream work when supported, then settle any reported usage.

SSE parsing accepts both LF and CRLF event separators. Observing a terminal
`[DONE]` event establishes provider-stream completion even when the downstream
client closes before the upstream socket reaches EOF. Settlement work is
shielded from that downstream cancellation. A disconnect without terminal usage
remains conservative and retains its reservation rather than inventing usage.

## Security invariants

- Provider credentials never leave Northgate and are encrypted at rest.
- Application keys are stored as one-way digests.
- Caller metadata does not grant authorization.
- Logs and errors never contain authorization headers or provider credentials.
- Prompt and response content is not persisted unless a gateway explicitly opts in.
