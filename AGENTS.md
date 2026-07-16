# Agent Notes

These instructions apply to coding agents working on Northgate.

## Read before changing code

Always start with `docs/README.md`, then follow its task-based routing table.
Do not load every document by default.

Before implementation exists, treat all API paths, table names, configuration
keys, and technology choices as proposals unless a document explicitly marks
them as accepted.

## Documentation rules

- Keep one canonical page for each concept. Link to it instead of duplicating it.
- Lead with the current decision or behavior, then explain rationale.
- Use exact terminology from `docs/product-scope.md`.
- Clearly label content as `proposed`, `accepted`, `implemented`, or `deprecated`.
- Update the owning document in the same change when a contract changes.
- Add an ADR only for a durable decision with meaningful alternatives and tradeoffs.

## Product boundaries

- Northgate is a standalone, provider-neutral service.
- Do not add Dayboard-specific domain concepts to Northgate.
- Do not add agent orchestration behavior that belongs in `north` or an application.
- Do not implement subscription conversion, provider account pooling, or credential resale.
- PostgreSQL is the durable source of truth; Redis may hold ephemeral policy state.
- Request and response bodies are not logged by default.

## Engineering defaults

- Preserve streaming end to end and avoid buffering SSE responses.
- Keep the data plane available when the management UI is unavailable.
- Authenticate metadata; never trust caller-supplied tenant or user identifiers by default.
- Keep provider credentials out of logs, traces, errors, and database plaintext.
- Prefer explicit provider adapters over lossy universal request rewriting.

