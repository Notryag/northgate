# Northgate documentation index

Status: accepted documentation structure  
Last reviewed: 2026-07-15

This page is the canonical entry point for Northgate documentation. Read only
the pages needed for the current task.

## Route by task

| Task | Required reading |
| --- | --- |
| Understand the product | [Product scope](product-scope.md) |
| Change service boundaries or request flow | [Product scope](product-scope.md), [Architecture](architecture.md) |
| Add or change a proxy endpoint | [Architecture](architecture.md), [API design](api-design.md) |
| Add authentication, limits, or credentials | [Architecture](architecture.md), [API design](api-design.md) |
| Plan implementation work | [Roadmap](roadmap.md), then the affected contract pages |
| Change code or verification workflow | [Development workflow](development.md) |
| Configure or migrate Northgate | [Configuration and migrations](operations/configuration-and-migrations.md) |
| Back up, restore, or upgrade | [Backup and restore](operations/backup-and-restore.md), [Upgrades](operations/upgrades.md) |
| Integrate Dayboard | [Integration boundaries](integration-boundaries.md), [API design](api-design.md) |
| Integrate an existing system | [Integration guide](integration-guide.md), [API design](api-design.md) |
| Investigate an open reliability gap | [Known issues and hardening work](known-issues.md) |
| Review durable technical decisions | [Architecture decisions](decisions/) |

## Canonical pages

- [Product scope](product-scope.md): users, responsibilities, non-goals, and terminology.
- [Architecture](architecture.md): components, ownership, request lifecycle, and invariants.
- [API design](api-design.md): proposed data-plane and control-plane contracts.
- [Integration boundaries](integration-boundaries.md): ownership split with Dayboard and `north`.
- [Integration guide](integration-guide.md): compatibility checks, canary traffic, and rollback.
- [Known issues and hardening work](known-issues.md): active reliability gaps and closure criteria.
- [Roadmap](roadmap.md): ordered milestones and completion criteria.
- [Development workflow](development.md): local verification cadence and completion gates.
- [Operations](operations/): configuration, migrations, backup, restore, and upgrade procedures.
- [Architecture decisions](decisions/): accepted decisions and their tradeoffs.

## Status vocabulary

- `proposed`: a working design that may change during implementation.
- `accepted`: an intentional decision that changes only through explicit review.
- `implemented`: behavior verified in the current codebase.
- `deprecated`: supported temporarily but should not be used for new integrations.

M0 through M3 are implemented. M4 existing-system adoption and M5 open-source
operations are in progress.
