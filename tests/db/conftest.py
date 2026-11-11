"""Postgres fixtures. Integration tests fail, never skip, when Postgres is absent."""

import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

from refinery_watch.db.connect import Conn, connect
from refinery_watch.db.migrate import upgrade
from refinery_watch.db.seed import load_refineries, load_sources

OWNER_URL = (
    "postgresql://refinery_test_owner:refinery_test_owner@127.0.0.1:5432/refinery_watch_test"
)
APP_URL = "postgresql://refinery_test_app:refinery_test_app@127.0.0.1:5432/refinery_watch_test"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SEED_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "seed"
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


def seed_schema(owner: str, schema: str) -> None:
    """Loads the small fixtures, one fixture source and R9001's CDU capacity, and commits."""
    with open_in_schema(owner, schema) as conn:
        load_sources(conn, SEED_DIR / "sources_small.csv")
        load_refineries(conn, SEED_DIR / "refineries_small.csv")
        conn.execute(
            "INSERT INTO source (id, name, tier, region_focus, access_method, feed_url,"
            " article_url_pattern, robots_status, fetch_interval_min)"
            " VALUES ('fixture_src', 'Fixture source', 2, 'Global', 'rss',"
            " 'https://example.test/feed', 'https://example.test/<id>', 'open', 60)"
        )
        conn.execute(
            "UPDATE refinery_unit SET capacity_kbd = 200"
            " WHERE refinery_id = 'R9001' AND unit_code = 'CDU'"
        )
        conn.commit()


@pytest.fixture(scope="session")
def session_schema(pg_server: str, app_url: str) -> Iterator[str]:
    name = make_schema(pg_server, app_url)
    seed_schema(pg_server, name)
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


def finish(conn: Conn) -> None:
    """Fires deferred checks so a test cannot leave a row without its primary source."""
    try:
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS:
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def db(session_schema: str, pg_server: str) -> Iterator[Conn]:
    """Owner connection inside a transaction that is rolled back."""
    conn = open_in_schema(pg_server, session_schema)
    yield conn
    finish(conn)


@pytest.fixture
def app_db(session_schema: str, app_url: str) -> Iterator[Conn]:
    """App-role connection inside a transaction that is rolled back."""
    conn = open_in_schema(app_url, session_schema)
    yield conn
    finish(conn)


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
