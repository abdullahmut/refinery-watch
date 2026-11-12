import psycopg
import pytest

from refinery_watch.db.connect import connect


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:hunter2-pw@127.0.0.1:1/db?connect_timeout=2",
        "postgresql://u:hunter2-pw@host with space:5432/db",
        "host=127.0.0.1 port=1 password=hunter2-pw bad option=1",
    ],
)
def test_connect_error_holds_no_dsn(url: str) -> None:
    with pytest.raises(psycopg.OperationalError) as info:
        connect(url)
    text = str(info.value)
    assert "hunter2-pw" not in text
    assert "127.0.0.1" not in text
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
