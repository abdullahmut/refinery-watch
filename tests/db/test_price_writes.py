from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from tests.db.helpers import open_in_schema

from refinery_watch.db.connect import Conn
from refinery_watch.sources.store import PriceRow, write_prices

pytestmark = pytest.mark.integration

ROWS = [
    PriceRow("eia.test1", date(2026, 11, 6), Decimal("90.1"), "percent"),
    PriceRow("eia.test2", date(2026, 11, 6), Decimal("16200"), "kbd"),
]


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def test_first_call_inserts_both_and_the_same_values_insert_none(app: Conn) -> None:
    assert write_prices(app, ROWS, "eia_wpsr", None) == 2
    assert write_prices(app, ROWS, "eia_wpsr", None) == 0
    count = app.execute("SELECT count(*) AS n FROM price").fetchone()
    assert count is not None
    assert count["n"] == 2


def test_a_changed_value_is_a_new_row_and_the_old_one_stays_visible_as_of(app: Conn) -> None:
    write_prices(app, ROWS, "eia_wpsr", None)
    cut = app.execute("SELECT clock_timestamp() AS t").fetchone()
    assert cut is not None
    revised = [PriceRow("eia.test1", date(2026, 11, 6), Decimal("90.5"), "percent"), ROWS[1]]
    assert write_prices(app, revised, "eia_wpsr", None) == 1
    as_of = app.execute(
        "SELECT value FROM price WHERE series = 'eia.test1' AND known_at <= %s"
        " ORDER BY known_at DESC LIMIT 1",
        (cut["t"],),
    ).fetchone()
    latest = app.execute(
        "SELECT value FROM price WHERE series = 'eia.test1' ORDER BY known_at DESC LIMIT 1"
    ).fetchone()
    assert as_of is not None
    assert latest is not None
    assert as_of["value"] == Decimal("90.1")
    assert latest["value"] == Decimal("90.5")


def test_value_with_extra_places_compares_at_column_scale(app: Conn) -> None:
    write_prices(app, ROWS[:1], "eia_wpsr", None)
    same = [PriceRow("eia.test1", date(2026, 11, 6), Decimal("90.10001"), "percent")]
    assert write_prices(app, same, "eia_wpsr", None) == 0


def test_a_new_week_is_a_new_row(app: Conn) -> None:
    write_prices(app, ROWS[:1], "eia_wpsr", None)
    later = [PriceRow("eia.test1", date(2026, 11, 13), Decimal("90.1"), "percent")]
    assert write_prices(app, later, "eia_wpsr", None) == 1


def test_no_update_or_delete_statements_in_the_adapter_code() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "refinery_watch"
    files = [*root.joinpath("sources").glob("*.py"), root / "objects.py"]
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.upper()
            assert "DELETE FROM" not in text, path
            assert "INTO OUTAGE_EVENT" not in text, path
            if "UPDATE " in text and "SET" in text:
                assert "JOB_RUN" in text, (path, line)
