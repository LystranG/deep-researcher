FROM ghcr.io/astral-sh/uv:0.12.1 AS uv
FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock alembic.ini ./
COPY apps/api ./apps/api
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn deep_researcher.app:app --host 0.0.0.0 --port 8000"]

