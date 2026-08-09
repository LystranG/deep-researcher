.PHONY: dev-api dev-web migrate test test-api test-web lint typecheck build verify

dev-api:
	rtk uv run uvicorn deep_researcher.app:app --reload --host 127.0.0.1 --port 8000

dev-web:
	rtk npm --prefix apps/web run dev

migrate:
	rtk uv run alembic upgrade head

test: test-api test-web

test-api:
	rtk uv run pytest apps/api/tests -q

test-web:
	rtk npm --prefix apps/web run test

lint:
	rtk uv run ruff check apps/api/src apps/api/tests

typecheck:
	rtk uv run mypy apps/api/src/deep_researcher
	rtk npm --prefix apps/web run typecheck

build:
	rtk npm --prefix apps/web run build

verify: test lint typecheck build
	rtk git diff --check

