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
- Release notes must call out new required settings, default changes, and any
  setting that changes request or cost behavior.

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
