# Needs GNU Make 4.x, uv and bash. On Windows run from Git Bash.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := test
.PHONY: up test lint deploy backup restore

test:
	uv run --locked pytest --cov --cov-report=term-missing

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy

up:
	echo "up: not built. No Compose file exists yet." >&2
	exit 1

deploy:
	echo "deploy: not built. infra/ does not exist yet. Operator runs this by hand." >&2
	exit 1

backup:
	echo "backup: not built. infra/ does not exist yet. Operator runs this by hand." >&2
	exit 1

restore:
	echo "restore: not built. infra/ does not exist yet. Operator runs this by hand." >&2
	exit 1
