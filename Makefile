# Needs GNU Make 4.x, uv and bash. On Windows run from Git Bash.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := test
.PHONY: up down test test-integration lint tf-check deploy backup restore

test:
	uv run --locked pytest -m "not integration" --cov --cov-report=term-missing --cov-fail-under=0

test-integration:
	uv run --locked pytest --cov --cov-report=term-missing

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy

tf-check:
	terraform -chdir=infra/terraform fmt -check -recursive -diff
	terraform -chdir=infra/terraform init -backend=false -input=false -lockfile=readonly
	terraform -chdir=infra/terraform validate

COMPOSE_LOCAL := docker compose -p refinery-watch-local --env-file .env -f infra/compose/data/compose.yaml -f infra/compose/data/compose.local.yaml

up:
	[ -f .env ] || { echo "up: .env not found at the repo root. Copy .env.example to .env and fill it in." >&2; exit 1; }
	docker info >/dev/null 2>&1 || { echo "up: Docker is not running. Start Docker Desktop, then run make up again." >&2; exit 1; }
	$(COMPOSE_LOCAL) up -d --wait --wait-timeout 180

down:
	docker info >/dev/null 2>&1 || { echo "down: Docker is not running. Nothing to stop." >&2; exit 1; }
	$(COMPOSE_LOCAL) down

deploy:
	echo "deploy: not built. Operator runs this by hand." >&2
	exit 1

backup:
	echo "backup: not built. Operator runs this by hand." >&2
	exit 1

restore:
	echo "restore: not built. Operator runs this by hand." >&2
	exit 1
