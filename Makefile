# Needs GNU Make 4.x, uv and bash. On Windows run from Git Bash.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := test
.PHONY: test lint

test:
	uv run --locked pytest --cov --cov-report=term-missing

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy
