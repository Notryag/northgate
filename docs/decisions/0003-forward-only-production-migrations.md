# ADR 0003: Forward-only production migrations

Status: accepted  
Date: 2026-07-17

## Context

Northgate stores encrypted provider credentials, routing configuration, policy
definitions, pricing, and the auditable request ledger in PostgreSQL. A partial
or ambiguous schema rollback can make both the data plane and accounting data
unreliable.

Alembic supports downgrade functions, but a syntactically reversible migration
does not guarantee that transformed or deleted production data can be restored.
Automatically migrating during application startup also creates races when
multiple replicas start together and removes operator control over backups.

## Decision

- Production migrations are forward-only and form one linear Alembic history.
- Northgate never runs migrations automatically during application startup.
- Every production upgrade takes and verifies a PostgreSQL backup before the
  application is stopped and the schema is advanced.
- Production rollback restores the pre-upgrade database backup and runs the
  matching prior Northgate version. Alembic downgrade is for local development
  and migration authoring only.
- Until a release explicitly documents rolling compatibility, schema upgrades
  use a maintenance window with all Northgate replicas stopped.
- New migrations should use expand-and-contract changes when practical and
  must not introduce a second migration head.

## Consequences

Upgrades have a short planned outage and require enough storage for a verified
backup. In exchange, rollback has a concrete recovery point and cannot silently
discard data through an incomplete downgrade. A future zero-downtime policy
will require explicit cross-version compatibility tests before replacing this
decision.

## Rejected alternatives

- Automatic startup migrations: rejected because replicas can race and backup
  policy becomes implicit.
- Production Alembic downgrade: rejected because schema reversal is not data
  recovery.
- Multiple migration branches: rejected because deployment ordering becomes
  ambiguous for self-hosters.
