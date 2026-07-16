FROM python:3.11-slim AS build

ARG UV_VERSION=0.11.28

ENV UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.11-slim

ENV PATH=/app/.venv/bin:$PATH \
    HOME=/home/northgate \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system northgate \
    && useradd --system --create-home --home-dir /home/northgate \
        --gid northgate --shell /usr/sbin/nologin northgate

COPY --from=build --chown=northgate:northgate /app/.venv ./.venv
COPY --chown=northgate:northgate alembic.ini ./alembic.ini
COPY --chown=northgate:northgate migrations ./migrations

USER northgate

EXPOSE 8080

CMD ["northgate"]

