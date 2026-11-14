import psycopg
import pytest
from psycopg import errors

from refinery_watch.db.connect import _category, connect


@pytest.mark.parametrize(
    ("url", "category"),
    [
        ("postgresql://u:hunter2-pw@127.0.0.1:1/db?connect_timeout=2", "refused|timeout"),
        ("postgresql://u:hunter2-pw@host with space:5432/db", "other"),
        ("host=127.0.0.1 port=1 password=hunter2-pw bad option=1", "other"),
    ],
)
def test_connect_error_holds_no_dsn(url: str, category: str) -> None:
    with pytest.raises(psycopg.OperationalError) as info:
        connect(url)
    text = str(info.value)
    assert text.removeprefix("could not connect to the database: ") in category.split("|")
    assert "hunter2-pw" not in text
    assert "127.0.0.1" not in text
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (errors.InvalidPassword("x"), "authentication failed"),
        (errors.InvalidAuthorizationSpecification("x"), "authentication failed"),
        (errors.QueryCanceled("x"), "timeout"),
        (psycopg.OperationalError("connection timeout expired"), "timeout"),
        (psycopg.OperationalError("connection failed: Connection refused"), "refused"),
        (
            psycopg.OperationalError("FATAL: password authentication failed for user"),
            "authentication failed",
        ),
        (psycopg.OperationalError("something else"), "other"),
    ],
)
def test_category(exc: psycopg.Error, category: str) -> None:
    assert _category(exc) == category
