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
