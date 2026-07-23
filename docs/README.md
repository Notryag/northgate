# Northgate documentation index

Status: accepted documentation structure  
Last reviewed: 2026-07-23

This page is the canonical entry point for Northgate documentation. Read only
the pages needed for the current task.

Start with [Current implementation state](current-state.md). It distinguishes
implemented behavior from accepted and proposed work. Coding agents should also
follow the repository-level [`AGENTS.md`](../AGENTS.md).

## Authority and document roles

When pages disagree, use this order and fix the stale page in the same change:

1. source code, migrations, and executable tests;
2. [Current implementation state](current-state.md);
3. accepted architecture and ADRs;
4. operations contracts for deployed workflows;
5. roadmap and architecture-review sequencing;
6. known-issue incident history and the development verification log.

`api-design.md` contains both implemented and proposed contracts. A listed route
is not implemented unless the current-state page, source, and tests agree.
`development.md` is append-oriented evidence and may describe behavior at an
older revision.

## Route by task

| Task | Required reading |
| --- | --- |
| Understand the current service | [Current state](current-state.md), [Product scope](product-scope.md) |
| Change service boundaries or request flow | [Current state](current-state.md), [Architecture](architecture.md) |
| Add or change a proxy endpoint | [Architecture](architecture.md), [API design](api-design.md) |
| Add authentication, limits, or credentials | [Architecture](architecture.md), [API design](api-design.md) |
| Change token admission or reservation | [Token admission reservation](token-reservation.md), [API design](api-design.md) |
| Plan implementation work | [Roadmap](roadmap.md), then the affected contract pages |
| Review architecture improvement priorities | [Architecture review](architecture-review.md), [Roadmap](roadmap.md) |
| Change code or verification workflow | [Development workflow](development.md) |
| Configure or migrate Northgate | [Configuration and migrations](operations/configuration-and-migrations.md) |
| Back up or upgrade | [Backup and restore](operations/backup-and-restore.md), [Upgrades](operations/upgrades.md) |
| Integrate Dayboard | [Integration boundaries](integration-boundaries.md), [API design](api-design.md) |
| Use the diagnostics CLI or MCP server | [Operator CLI and MCP usage](operator-cli-and-mcp.md), [Operator diagnostics interface](diagnostics-interface.md) |
| Add AI-assisted operator diagnostics | [Operator diagnostics interface](diagnostics-interface.md), [Known issues](known-issues.md) |
| Change the operator console | [Operator console](console.md), [Current state](current-state.md), [API design](api-design.md) |
| Integrate an existing system | [Integration guide](integration-guide.md), [API design](api-design.md) |
| Investigate an open reliability gap | [Known issues and hardening work](known-issues.md) |
| Review durable technical decisions | [Architecture decisions](decisions/) |

## Canonical pages

- [Current implementation state](current-state.md): compact implemented behavior,
  runtime boundaries, invariants, verification baseline, and explicitly absent work.
- [Product scope](product-scope.md): users, responsibilities, non-goals, and terminology.
- [Architecture](architecture.md): components, ownership, request lifecycle, and invariants.
- [API design](api-design.md): proposed data-plane and control-plane contracts.
- [Integration boundaries](integration-boundaries.md): ownership split with Dayboard and `north`.
- [Integration guide](integration-guide.md): compatibility checks, canary traffic, and rollback.
- [Operator diagnostics interface](diagnostics-interface.md): implemented shared service, REST, CLI, and read-only MCP diagnostics contract.
- [Token admission reservation](token-reservation.md): prompt estimation,
  output-limit precedence, reservation formula, settlement, and diagnostics.
- [Operator CLI and MCP usage](operator-cli-and-mcp.md): installation,
  authentication, command examples, MCP client registration, and troubleshooting.
- [Operator console](console.md): accepted management information architecture,
  visual contract, frontend stack, and phased delivery order.
- [Known issues and hardening work](known-issues.md): active gaps first, then incident history and closure criteria.
- [Architecture review](architecture-review.md): accepted assessment and ordered refactoring work.
- [Roadmap](roadmap.md): ordered milestones and completion criteria.
- [Development workflow](development.md): verification cadence followed by an append-oriented historical log.
- [Operations](operations/): configuration, migrations, backup, restore, and upgrade procedures.
- [Architecture decisions](decisions/): accepted decisions and their tradeoffs.

## Status vocabulary

- `proposed`: a working design that may change during implementation.
- `accepted`: an intentional decision that changes only through explicit review.
- `implemented`: behavior verified in the current codebase.
- `deprecated`: supported temporarily but should not be used for new integrations.

M0 through M3 are implemented. M4 existing-system adoption and M5 open-source
operations are in progress.
