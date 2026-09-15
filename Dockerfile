# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv ./.venv
COPY --from=builder --chown=app:app /app/app ./app

USER app

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT}\""]
