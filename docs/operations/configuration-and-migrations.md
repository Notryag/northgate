# Configuration and migration policy

Status: accepted and implemented  
Last reviewed: 2026-07-17

## Configuration contract

Northgate configuration is environment-backed and uses the `NORTHGATE_`
prefix. `.env.example` is the canonical inventory for Northgate-specific
settings; README sections describe behavior and safe defaults.

- New optional settings must have backward-compatible defaults.
- Empty environment values are ignored so Compose can represent an unset
  optional value; non-empty invalid values still fail validation.
- A renamed or removed setting must be documented as deprecated for at least
  one minor release before removal.
- Invalid values for known settings fail during settings construction. Features
  that require paired settings, such as tracing and its OTLP endpoint, fail
  before the application starts serving traffic.
- Secrets are supplied through a deployment secret manager, not committed
  environment files. Secret values must not appear in logs, metrics, traces, or
  error responses.
- `northgate-inspect` client settings use the separate `NORTHGATE_INSPECT_*`
  namespace. Supply its raw operator credential through the environment or a
  regular `NORTHGATE_INSPECT_OPERATOR_KEY_FILE` with no group/other access; never
  pass it as a command argument.
- Release notes must call out new required settings, default changes, and any
  setting that changes request or cost behavior.
- `NORTHGATE_MAX_REQUEST_BODY_BYTES` bounds buffered proxy request bodies and
  defaults to 5 MiB. Both `Content-Length` requests and chunked bodies are
  rejected with `413 REQUEST_TOO_LARGE` once the limit is exceeded.
- `NORTHGATE_SETTLEMENT_OUTBOX_ENABLED` defaults to `false` and requires
  `NORTHGATE_USAGE_PERSISTENCE_ENABLED=true`. Enable it only after revision
  `0013`, a continuously running `northgate-worker`, metrics scraping, and outbox
  alerts are present.
- `NORTHGATE_SETTLEMENT_WORKER_HEARTBEAT_TTL_SECONDS` defaults to 15 seconds.
  Continuous workers refresh instance-specific Redis keys.
- `NORTHGATE_SETTLEMENT_READINESS_MAX_PENDING_AGE_SECONDS` defaults to 300
  seconds. When no worker heartbeat is visible, readiness remains available but
  degraded until the oldest recoverable event exceeds this age; an overdue
  backlog returns `503`.
- `NORTHGATE_SETTLEMENT_COMPLETED_RETENTION_DAYS` defaults to 30 days and is used
  by `northgate-worker --cleanup-completed`. Cleanup deletes only completed
  events, in bounded batches; retryable and failed events are retained.

The following material is not recoverable from PostgreSQL and must be retained
in the deployment secret manager:

- `NORTHGATE_CREDENTIAL_ENCRYPTION_KEY`
- raw application, operator, and metrics keys whose SHA-256 digests are stored
- provider API keys used by configuration routing
- OTLP exporter credentials

Changing `NORTHGATE_CREDENTIAL_ENCRYPTION_KEY` without an explicit credential
rotation procedure makes restored encrypted provider credentials unreadable.

## Migration contract

The production policy is defined by
[ADR 0003](../decisions/0003-forward-only-production-migrations.md).

- `uv run alembic heads` must report exactly one head.
- The deployed schema is inspected with `uv run alembic current`.
- The application does not run `alembic upgrade` during startup.
- Apply migrations with the target application version, after a verified
  backup and while all Northgate replicas are stopped.
- Do not run Alembic downgrade in production. Restore the pre-upgrade backup
  and prior application version instead.

Migration revisions must include upgrade and downgrade functions so developers
can iterate locally, even though production rollback uses restore. PostgreSQL
transactional DDL should be preserved; a migration requiring non-transactional
operations must document its recovery procedure in the release notes.

Revision `0012` creates `settlement_events`, and revision `0013` allows multiple
idempotent event keys per request. Both are required by this version of
`northgate-worker` and must be applied before it starts.

Revision `0014` adds application-key fixed metadata and marks existing keys as
`legacy`; new keys default to `trusted`. Before removing legacy behavior, issue
replacement keys with the required `fixed_metadata`, move each application,
verify route selection, and revoke the old keys. Roll back the application version
and restore the pre-upgrade database backup if trusted routing causes an unexpected
outage.

Revision `0015` adds nullable metadata trust classifications to request records.
Existing historical records remain unclassified and are excluded from trusted
tenant aggregation; no historical trust value is inferred.

Revision `0016` adds `schema_version: 1` to existing settlement payloads and a
partial worker-queue index over recoverable events. New workers reject unsupported
payload versions instead of interpreting them with the current schema.

Local downgrade from `0013` to `0012` requires removing all but one settlement
event per request first. Production rollback remains backup restore plus the prior
application version; do not delete settlement history or downgrade in place.
