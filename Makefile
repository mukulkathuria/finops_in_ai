# Developer shortcuts. Every target wraps a documented command (see README).
.PHONY: setup env up down logs migrate test test-unit test-integration lint fmt typecheck imports audit check

setup:
	uv sync
env:
	uv run python scripts/init_env.py
up:
	docker compose up -d --build
down:
	docker compose down
logs:
	docker compose logs -f --tail=100
migrate:
	uv run alembic upgrade head
test:
	uv run pytest
test-unit:
	uv run pytest tests/unit tests/contract
test-integration:
	uv run pytest -m integration
lint:
	uv run ruff check . && uv run ruff format --check .
fmt:
	uv run ruff format . && uv run ruff check --fix .
typecheck:
	uv run mypy
imports:
	uv run lint-imports
audit:
	uv run python scripts/audit_rls.py && uv run python scripts/audit_routes.py
check: lint typecheck imports test-unit
