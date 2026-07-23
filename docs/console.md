# Operator console

Status: phase 2 implemented
Last reviewed: 2026-07-23

The Northgate console is an operator workspace for configuration, usage, and
incident diagnosis. It is not a marketing site and must remain outside the data
plane's availability boundary.

## Information architecture

The accepted navigation is:

| Area | Responsibility |
| --- | --- |
| Overview | Traffic, tokens, cost, latency, failures, and current operational state |
| Requests | Recent requests, correlation search, provider attempts, settlement events, and findings |
| Gateways | Gateway configuration, routes, weights, metadata matching, and policy |
| Applications | Organizations, projects, application keys, fixed metadata, and revocation |
| Providers | Redacted provider credentials, adapters, rotation, and route references |
| Usage | Tenant, route, provider, model, and time-bucket analysis |
| Operations | Stale settlement, worker backlog, leases, readiness, and recovery evidence |
| Pricing | Append-only model price versions |

Phase 1 implements the shared shell, Overview, Requests, Usage, and Pricing.
Phase 2 implements Gateway selection and creation, Route creation and bounded
traffic-field updates, trusted metadata match configuration, and Gateway Policy
replacement. Applications, Provider credential writes, and Operations remain in
the ordered backlog.

Gateway route create/edit forms also configure the nullable default output token
reservation. Request detail displays the prompt estimate, output reserve, attempt
multiplier, margin, total reserve, actual/released values, ratio, estimator, and
output-limit source. Missing provider usage remains visibly unknown.

The implemented request workspace lists up to 100 recent records without
requiring a correlation filter, supports paired metadata filtering, opens a
stable request URL, and renders request fields, findings, provider attempts, and
redacted settlement progress. FastAPI serves the SPA index for nested Console
routes so direct links and browser refreshes remain valid.

## Interaction and visual contract

- Use a dark neutral sidebar and a light, dense work surface.
- Green means healthy, amber means degraded or retrying, and red is reserved for
  failures or destructive actions.
- Request IDs, route IDs, provider request IDs, and other machine identifiers use
  a monospace face and remain easy to copy.
- Prefer dense tables and explicit timelines over decorative cards or excessive
  charts. Charts must answer a specific traffic, token, cost, or latency question.
- Request details must preserve the Request -> Attempt -> Settlement sequence and
  display diagnostic findings without exposing prompts, responses, tool payloads,
  metadata values, or credentials.
- Configuration writes require an explicit submit action. Secret creation or
  rotation must never redisplay an existing secret; one-time application keys are
  handled as one-time values.

Sub2API is a reference for CRUD and configuration flow, Sentry for request and
attempt investigation, Stripe for usage and pricing, Grafana for health state,
and Cloudflare for gateway and route organization. These are interaction
references, not a reason to copy their branding or marketing layouts.

## Frontend stack

- React and Vite;
- React Router for durable page URLs;
- TanStack Query for server state and invalidation;
- Ant Design for accessible management controls and dense tables;
- Recharts for the existing bounded usage chart;
- Zod for response and form boundary validation;
- React Hook Form for mutation forms.

The operator key remains in session storage for the current self-hosted
deployment. It is never accepted through a URL or persisted in local storage.
An authenticated server-side console session and role-separated capabilities are
future security work, not implied by the frontend framework.

## Delivery order

1. Shared application shell, access dialog, routing, query client, and API error handling.
2. Recent-request list, correlation search, and request diagnostic detail.
3. Existing overview, usage, tenant, route, and pricing workflows migrated from the single page.
4. Completed on 2026-07-22: Gateway, route, and policy management.
5. Organization, project, application-key, and provider-credential management.
6. Operations workspace and Web browser regression coverage.
