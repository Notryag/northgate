# Northgate

Northgate is a planned open-source AI gateway for observability, routing,
rate limiting, and cost control. Applications send model traffic through
Northgate instead of integrating provider governance separately.

Northgate is currently in the foundation phase. The repository contains a
runnable service shell and the initial product and architecture proposals; it
does not contain proxy behavior yet.

## Core responsibilities

- Proxy streaming and non-streaming AI requests.
- Authenticate applications without exposing upstream provider credentials.
- Record requests, tokens, cost, latency, cache status, and errors.
- Enforce request, token, concurrency, and spend policies.
- Route requests across providers and models with explicit fallback rules.
- Export metrics, traces, and audit events.

Northgate is not an account pool, subscription conversion service, model
runtime, or agent framework.

## Documentation

Start at [docs/README.md](docs/README.md). It routes readers and coding agents
to the smallest relevant set of documents.

## Project status

Status: M2 limits and analytics complete; M3 routing and reliability next

The transparent proxy, Redis-backed limits, spend accounting, analytics APIs,
and React operator console are implemented. See [docs/roadmap.md](docs/roadmap.md).

## Development

Install dependencies and run the checks:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Run the service locally:

```sh
cp .env.example .env
uv run northgate
```

The liveness endpoint is `http://127.0.0.1:8080/health/live`. The current
readiness endpoint covers the process only; dependency checks will be added
when the request path begins using PostgreSQL and Redis.

Configure the first OpenAI-compatible gateway by setting an application key
digest and provider credential in `.env`:

```sh
printf '%s' 'ng_live_example' | sha256sum
# Put the digest in NORTHGATE_APPLICATION_KEY_SHA256 and set
# NORTHGATE_PROVIDER_API_KEY. Do not put either credential in source control.
```

Then send chat completions to:

```text
POST /v1/gateways/default/openai/chat/completions
Authorization: Bearer ng_live_example
```

Configuration routing is useful for the first smoke test. To move the same
gateway into durable storage:

```sh
# Generate once and store the output in NORTHGATE_CREDENTIAL_ENCRYPTION_KEY.
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

uv run alembic upgrade head
uv run northgate-bootstrap
```

After bootstrap succeeds, set these values and restart Northgate:

```text
NORTHGATE_ROUTING_SOURCE=database
NORTHGATE_USAGE_PERSISTENCE_ENABLED=true
```

Optional gateway limits are persisted by the same bootstrap command:

```text
NORTHGATE_REQUEST_LIMIT_PER_MINUTE=60
NORTHGATE_CONCURRENCY_LIMIT=10
NORTHGATE_TOKEN_LIMIT_PER_DAY=1000000
NORTHGATE_DAILY_SPEND_LIMIT_MICROUSD=5000000
NORTHGATE_MONTHLY_SPEND_LIMIT_MICROUSD=100000000
```

Token capacity is reserved before forwarding. The estimate is request UTF-8
bytes divided by three plus `max_completion_tokens`/`max_tokens`, or 4096 when
the request omits an output cap. Northgate settles the reservation to provider-
reported usage exactly once by request ID. When usage is missing after an
ambiguous provider failure, the conservative reservation remains charged.

Spend limits require a versioned price. Bootstrap can establish the initial
price version using `NORTHGATE_PRICE_PROVIDER`, `NORTHGATE_PRICE_MODEL`, and the
input/output `*_PRICE_MICROUSD_PER_MILLION` values. One US dollar is 1,000,000
micro-USD.

Usage analytics require a separate operator key digest:

```text
GET /api/v1/usage/summary
GET /api/v1/usage/timeseries?interval=hour
Authorization: Bearer <operator key>
```

Application keys cannot call operator analytics endpoints.

The React operator console is available at `/console`. For frontend
development with API requests proxied to the local service:

```sh
cd apps/console
npm ci
npm run dev
```

`northgate-bootstrap` is idempotent for the default organization, project,
gateway, key, credential, and route. It reads secrets from the environment,
stores only the application-key digest, and encrypts the provider credential
before writing it to PostgreSQL.

For an OpenAI-compatible client, use this base URL and the Northgate
application key in place of the provider key:

```text
OPENAI_BASE_URL=http://127.0.0.1:8080/v1/gateways/default/openai
OPENAI_API_KEY=ng_live_example
```

The client continues to append `/chat/completions`; tool definitions, tool
calls, and tool results pass through unchanged.

Run the isolated development stack:

```sh
docker compose up --build
```

PostgreSQL is exposed on port 5433 and Redis on port 6380 to avoid colliding
with the shared platform services. Apply migrations with:

```sh
uv run alembic upgrade head
```
