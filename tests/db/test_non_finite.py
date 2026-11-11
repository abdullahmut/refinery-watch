"""NaN and infinity pass `x > 0`, so every numeric column needs an explicit finite check."""

from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from tests.db.helpers import add_document, day, insert_raw_outage, ts, unit_id

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import NewEventRow, Review, SourceLink, write_event
from refinery_watch.db.seed import SeedError, load_refineries

pytestmark = pytest.mark.integration

HEADER = (
    "refinery_id,name,aliases,country,region,owner,nameplate_kbd,major_units,status,source_url,"
    "verified,verified_on,notes\n"
)
NON_FINITE = ["NaN", "Infinity", "-Infinity"]
BAD_NUMBER = (psycopg.errors.CheckViolation, psycopg.errors.NumericValueOutOfRange)


def refused(db: Conn, sql: str, params: tuple[object, ...] = ()) -> None:
    with pytest.raises(BAD_NUMBER), db.transaction():
        db.execute(sql, params)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_capacity_affected_kbd(db: Conn, bad: str) -> None:
    with pytest.raises(BAD_NUMBER), db.transaction():
        insert_raw_outage(db, capacity_affected_kbd=Decimal(bad))


def test_write_event_sends_nan_capacity_to_review(db: Conn) -> None:
    row = NewEventRow(
        "unplanned",
        "R9001",
        unit_id(db),
        False,
        day(1),
        "high",
        "stated",
        100,
        capacity_affected_kbd=Decimal("NaN"),
        known_at=ts(1),
    )
    link = SourceLink(add_document(db, 950), "company_statement", "q")
    result = write_event(db, row, link)
    assert isinstance(result, Review)
    assert result.reason == "invalid_capacity"


@pytest.mark.parametrize("bad", NON_FINITE)
def test_reference_capacities(db: Conn, bad: str) -> None:
    refused(db, "UPDATE refinery SET nameplate_kbd = %s WHERE id = 'R9001'", (Decimal(bad),))
    refused(
        db,
        "UPDATE refinery_unit SET capacity_kbd = %s WHERE refinery_id = 'R9001'",
        (Decimal(bad),),
    )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_price_value(db: Conn, bad: str) -> None:
    refused(
        db,
        "INSERT INTO price (series, observed_on, value, unit, source_id)"
        " VALUES ('x', '2025-11-01', %s, 'pct', 'fixture_src')",
        (Decimal(bad),),
    )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_extraction_cost(db: Conn, bad: str) -> None:
    doc = add_document(db)
    refused(
        db,
        "INSERT INTO extraction (raw_document_id, model, prompt_version, tokens_in, tokens_out,"
        " cost_usd, status, output) VALUES (%s, 'm', 'extract_v1', 1, 1, %s, 'ok', '{}')",
        (doc, Decimal(bad)),
    )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_note_summary_cost(db: Conn, bad: str) -> None:
    refused(
        db,
        "INSERT INTO note (note_date, as_of, status, figures, body_text, body_html, summary,"
        " summary_model, summary_called_at, summary_tokens_in, summary_tokens_out,"
        " summary_cost_usd) VALUES ('2025-11-10', %s, 'built', '{}', 't', 't', 's', 'm', %s,"
        " 1, 1, %s)",
        (ts(10), ts(10), Decimal(bad)),
    )


@pytest.mark.parametrize("bad", [*NON_FINITE, "nan", "inf", "-inf"])
def test_seed_rejects_non_finite_nameplate(db: Conn, tmp_path: Path, bad: str) -> None:
    path = tmp_path / "refineries_bad.csv"
    path.write_text(
        HEADER + f"R9100,Bad Plant,,Nowhere,Other,,{bad},CDU,operating,,no,,\n", encoding="utf-8"
    )
    with pytest.raises(SeedError, match="nameplate_kbd"):
        load_refineries(db, path)
