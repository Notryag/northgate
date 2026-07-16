# ADR 0002: Initial operator API authentication

Status: accepted
Date: 2026-07-16

## Decision

The initial operator API uses a dedicated bearer key configured as a SHA-256
digest. It is separate from data-plane application keys and is required for all
cross-project analytics endpoints.

## Rationale

Application keys identify traffic-producing projects and must not grant access
to organization-wide usage or cost data. A dedicated operator key provides a
small, auditable control-plane boundary before user accounts and roles exist.

## Consequences

- Deployments must rotate and distribute the operator key as an administrative secret.
- The key digest may be stored in environment-backed secret configuration; plaintext is not stored.
- Fine-grained users, roles, and project-scoped operator permissions remain a later control-plane decision.
