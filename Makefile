.PHONY: dev-api dev-web migrate test test-api test-web lint typecheck build verify

dev-api:
	uv run uvicorn deep_researcher.app:app --reload --host 127.0.0.1 --port 8000

dev-web:
	npm --prefix apps/web run dev

migrate:
	uv run alembic upgrade head

test: test-api test-web

test-api:
	uv run pytest apps/api/tests -q

test-web:
	npm --prefix apps/web run test

lint:
	uv run ruff check apps/api/src apps/api/tests

typecheck:
	uv run mypy apps/api/src/deep_researcher
	npm --prefix apps/web run typecheck

build:
	npm --prefix apps/web run build

verify: test lint typecheck build
	git diff --check

