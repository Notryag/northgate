# ADR 0001: Python service stack

Status: accepted  
Date: 2026-07-16

## Decision

Northgate starts with Python 3.11, FastAPI/Starlette ASGI, Uvicorn, SQLAlchemy
with asyncpg, Alembic, and the async Redis client. The repository uses `uv` for
dependency and environment management, Ruff for linting and formatting, and
pytest for tests.

Request-path code must remain asynchronous. Streaming middleware and provider
adapters must not consume or assemble complete response bodies. Blocking SDKs,
synchronous database calls, and CPU-heavy work are not permitted on the event
loop.

## Rationale

Python is already the operating language for Dayboard and `north`, so it gives
the current maintainers the shortest path to a correct first implementation.
ASGI supports the initial concurrency and streaming requirements without
introducing a second service language before load characteristics are known.

Go would provide simpler deployment and more predictable resource use at high
concurrency, but those benefits do not currently outweigh the delivery and
maintenance cost. Northgate remains a standalone service and does not share
application types or tables with the Python projects.

## Consequences

- Streaming, cancellation, and connection-leak tests are release requirements.
- Load tests must be established before M1 is declared complete.
- A language change requires measured evidence that the Python data plane cannot
  meet documented concurrency, latency, or operational targets.

