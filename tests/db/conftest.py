"""Postgres fixtures. Integration tests fail, never skip, when Postgres is absent."""

import os
from collections.abc import Callable
from urllib.parse import urlparse

import psycopg
import pytest

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
