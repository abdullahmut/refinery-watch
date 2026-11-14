from collections.abc import Callable

import pytest

from refinery_watch.db.connect import connect

pytestmark = pytest.mark.integration


def test_server_is_16(pg_server: str) -> None:
    with connect(pg_server) as conn:
        row = conn.execute("SELECT current_setting('server_version_num')::int AS v").fetchone()
    assert row is not None
    assert 160000 <= row["v"] < 170000


def test_session_defaults(pg_server: str) -> None:
    with connect(pg_server) as conn:
        row = conn.execute(
            "SELECT current_setting('transaction_isolation') AS iso,"
            " current_setting('application_name') AS app"
        ).fetchone()
        assert conn.autocommit is False
    assert row == {"iso": "read committed", "app": "refinery-watch"}


def test_remote_host_refused_before_connecting(local_guard: Callable[[str], None]) -> None:
    with pytest.raises(pytest.fail.Exception, match="local"):
        local_guard("postgresql://u:p@10.0.0.5:5432/db")


def test_local_hosts_accepted(local_guard: Callable[[str], None]) -> None:
    local_guard("postgresql://u:p@127.0.0.1:5432/db")
    local_guard("postgresql://u:p@localhost/db")
    local_guard("postgresql://u:p@[::1]/db")


@pytest.mark.parametrize("param", ["host=10.0.0.5", "hostaddr=10.0.0.5", "service=remote"])
def test_url_query_cannot_redirect_the_connection(
    local_guard: Callable[[str], None], param: str
) -> None:
    with pytest.raises(pytest.fail.Exception, match="query"):
        local_guard(f"postgresql://u:p@127.0.0.1:5432/db?{param}")


@pytest.mark.parametrize("name", ["PGHOST", "PGHOSTADDR", "PGSERVICE"])
def test_environment_cannot_redirect_the_connection(
    local_guard: Callable[[str], None], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "10.0.0.5")
    with pytest.raises(pytest.fail.Exception, match=name):
        local_guard("postgresql://u:p@127.0.0.1:5432/db")


def test_other_query_options_are_allowed(local_guard: Callable[[str], None]) -> None:
    local_guard("postgresql://u:p@127.0.0.1:5432/db?connect_timeout=5")


@pytest.mark.parametrize(
    ("host", "hostaddr"),
    [("10.0.0.5", "10.0.0.5"), ("127.0.0.1", "10.1.1.1"), ("db.example.test", "127.0.0.1")],
)
def test_connected_server_must_be_loopback(
    server_guard: Callable[[str, str], None], host: str, hostaddr: str
) -> None:
    with pytest.raises(pytest.fail.Exception, match="loopback"):
        server_guard(host, hostaddr)


@pytest.mark.parametrize(
    ("host", "hostaddr"),
    [("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("::1", "::1")],
)
def test_loopback_server_is_accepted(
    server_guard: Callable[[str, str], None], host: str, hostaddr: str
) -> None:
    server_guard(host, hostaddr)


def test_session_connection_is_loopback(
    pg_server: str, server_guard: Callable[[str, str], None]
) -> None:
    with connect(pg_server) as conn:
        server_guard(conn.info.host, conn.info.hostaddr)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@127.0.0.1,10.0.0.5:5432/db",
        "postgresql://u:p@localhost,evil.example.test/db",
        "postgresql://u:p@127.0.0.1:5432,5433/db",
    ],
)
def test_multi_host_url_is_refused(local_guard: Callable[[str], None], url: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="one host"):
        local_guard(url)


def test_bad_port_is_refused(local_guard: Callable[[str], None]) -> None:
    with pytest.raises(pytest.fail.Exception, match="port"):
        local_guard("postgresql://u:p@127.0.0.1:notaport/db")
