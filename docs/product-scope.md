# Product scope

Status: accepted  
Last reviewed: 2026-07-15

## What is Northgate?

Northgate is a self-hosted, open-source gateway placed between AI applications
and model providers. It gives operators one place to observe traffic, enforce
policies, manage provider credentials, and route requests.

The product category is comparable to Cloudflare AI Gateway. Northgate is an
independent implementation and does not depend on Cloudflare AI Gateway or
subscription-management gateways.

## Who is it for?

- Teams operating multiple AI-enabled applications.
- Developers who need an OpenAI-compatible integration path first.
- Operators who need per-application, per-user, or per-model usage visibility.
- Self-hosters who need control of logs, credentials, and retention.

## Core concepts

| Term | Meaning |
| --- | --- |
| Organization | Administrative owner of projects and users. |
| Project | An application or environment with isolated keys and usage. |
| Gateway | A named traffic entry point with routes and policies. |
| Application key | Credential an application uses to call Northgate. |
| Provider credential | Encrypted upstream credential used by a route. |
| Route | Ordered rule that selects a provider, endpoint, and model. |
| Policy | Limit, budget, cache, logging, or safety rule applied to traffic. |
| Request record | Durable metadata and usage outcome for one gateway request. |

## Required capabilities

- Provider-native and OpenAI-compatible request forwarding.
- Streaming without response buffering.
- Application authentication and provider credential isolation.
- Request, token, concurrency, and spend limits.
- Usage, cost, latency, error, and cache analytics.
- Explicit routing, retries, and fallbacks.
- Configurable metadata retention and content logging.
- Metrics, traces, and audit export.

## Non-goals

- Selling or sharing model-provider accounts.
- Converting consumer subscriptions into API access.
- Hosting or executing model inference.
- Agent planning, tool execution, memory, or workflow orchestration.
- Owning application authorization or business-domain policy.
- Replacing provider safety systems with unverifiable generic behavior.

## Product principles

1. The proxy path stays small, predictable, and observable.
2. Provider differences remain visible instead of being silently discarded.
3. Content logging is opt-in; metadata logging is minimized and documented.
4. Limits fail closed when cost exposure is possible and policy state is unavailable.
5. Every charged request has a stable request ID and an auditable usage outcome.
6. The data plane is operable without the web console.
