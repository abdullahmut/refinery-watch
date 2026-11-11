# 014: Forward-only migrations and a required test database

Date: 2026-11-11

## Context

The schema's main rules live in triggers and SQL functions, so they can only be tested against a
real Postgres. Tests that skip when it is missing would let a green build hide them. A downgrade
that drops `outage_event` would delete every row in it. The roles come from the data Compose
stack: `refinery_owner` runs migrations and cannot create roles or databases, and the app role's
name differs between production and the laptop test database.

## Options

- Plain SQL files and a small runner, or Alembic with SQLAlchemy models, or Alembic with raw SQL.
- Tests that skip without a database, or tests that fail loudly.
- A database per test session, or a schema per session inside the test database.
- A group role for app grants, or grants to the login role named by a parameter.

## Decision

Alembic with raw SQL through `op.execute`. SQLAlchemy is installed only because Alembic needs it.
Application code uses psycopg 3. Migrations are forward only: every `downgrade()` raises. They take
an `app_role` parameter, default `refinery_app`, checked against a plain identifier pattern, and
grant to that role directly. No migration creates a role.

`make test` runs offline tests and needs no Docker. `make test-integration` runs the full suite
against Postgres on 127.0.0.1:5432, holds the coverage threshold, and fails with "Postgres not
reachable on 127.0.0.1:5432. Run make up." Nothing skips. `TEST_DATABASE_URL` and
`TEST_APP_DATABASE_URL` default to the fixed, non-secret test roles in `refinery_watch_test`, so
nobody reads `.env`. Any host other than localhost is refused. Each session migrates a new schema,
`rw_test_<hex>`, and drops it at the end, because the test roles cannot create databases. CI runs
the same roles script and both targets.

## Consequences

A green CI run means the triggers and functions were tested. An offline run on the laptop can pass
with low coverage; CI cannot. A mistake costs one more migration. To change the skip rule, edit one
fixture. To drop Alembic, the SQL in each migration moves to plain files as it is.
