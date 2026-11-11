"""Postgres fixtures. Integration tests fail, never skip, when Postgres is absent."""

import os
import secrets
from collections.abc import Callable, Iterator
from urllib.parse import urlparse

import psycopg
import pytest

from refinery_watch.db.connect import Conn, connect
from refinery_watch.db.migrate import upgrade

OWNER_URL = (
    "postgresql://refinery_test_owner:refinery_test_owner@127.0.0.1:5432/refinery_watch_test"
)
APP_URL = "postgresql://refinery_test_app:refinery_test_app@127.0.0.1:5432/refinery_watch_test"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
NOT_REACHABLE = "Postgres not reachable on 127.0.0.1:5432. Run make up."


def assert_local(url: str) -> None:
    host = urlparse(url).hostname
    if host not in LOCAL_HOSTS:
        pytest.fail(f"test database host must be local, got {host!r}")


@pytest.fixture(scope="session")
def local_guard() -> Callable[[str], None]:
    return assert_local


@pytest.fixture(scope="session")
def owner_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", OWNER_URL)
    assert_local(url)
    return url


@pytest.fixture(scope="session")
def app_url() -> str:
    url = os.environ.get("TEST_APP_DATABASE_URL", APP_URL)
    assert_local(url)
    return url


@pytest.fixture(scope="session")
def pg_server(owner_url: str, app_url: str) -> str:
    """Checks the server once per session and returns the owner URL."""
    try:
        with psycopg.connect(owner_url, connect_timeout=5) as conn:
            version = conn.info.server_version
    except psycopg.OperationalError:
        pytest.fail(NOT_REACHABLE)
    if version < 160000:
        pytest.fail(f"Postgres 16 or newer is required, got {version}")
    return owner_url


def app_role_of(url: str) -> str:
    user = urlparse(url).username
    assert user is not None
    return user


def make_schema(owner: str, app: str) -> str:
    """Creates and migrates a throwaway schema; returns its name."""
    name = f"rw_test_{secrets.token_hex(4)}"
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
    upgrade(owner, app_role=app_role_of(app), schema=name)
    return name


def drop_schema(owner: str, name: str) -> None:
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


def open_in_schema(url: str, schema: str) -> Conn:
    conn = connect(url)
    conn.execute(f"SET search_path TO {schema}")
    return conn


@pytest.fixture(scope="session")
def session_schema(pg_server: str, app_url: str) -> Iterator[str]:
    name = make_schema(pg_server, app_url)
    yield name
    drop_schema(pg_server, name)


@pytest.fixture
def empty_schema(pg_server: str) -> Iterator[str]:
    """A new schema with nothing in it."""
    name = f"rw_test_{secrets.token_hex(4)}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
    yield name
    drop_schema(pg_server, name)


@pytest.fixture
def fresh_schema(pg_server: str, app_url: str) -> Iterator[str]:
    name = make_schema(pg_server, app_url)
    yield name
    drop_schema(pg_server, name)


@pytest.fixture
def db(session_schema: str, pg_server: str) -> Iterator[Conn]:
    """Owner connection inside a transaction that is rolled back."""
    conn = open_in_schema(pg_server, session_schema)
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def app_db(session_schema: str, app_url: str) -> Iterator[Conn]:
    """App-role connection inside a transaction that is rolled back."""
    conn = open_in_schema(app_url, session_schema)
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    yield conn
    conn.rollback()
    conn.close()


ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")


@pytest.fixture
def privileges(db: Conn, app_url: str) -> Callable[[str], set[str]]:
    """Returns the table privileges the app role holds on a table of the session schema."""
    role = app_role_of(app_url)

    def held(table: str) -> set[str]:
        out: set[str] = set()
        for privilege in ALL_PRIVILEGES:
            row = db.execute(
                "SELECT has_table_privilege(%s, %s, %s) AS h", (role, table, privilege)
            ).fetchone()
            assert row is not None
            if row["h"]:
                out.add(privilege)
        return out

    return held


@pytest.fixture
def fresh_db(fresh_schema: str, pg_server: str) -> Iterator[Conn]:
    """Owner connection to a migrated, empty schema. Nothing is seeded."""
    conn = open_in_schema(pg_server, fresh_schema)
    yield conn
    conn.rollback()
    conn.close()
