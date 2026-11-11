import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
GRANTS = {
    "refinery": {"SELECT"},
    "refinery_unit": {"SELECT"},
    "refinery_status_change": {"SELECT"},
    "source": {"SELECT"},
}


def add_refinery(db: Conn, **over: Any) -> None:
    row: dict[str, Any] = {
        "id": "R9001",
        "name": "Test Plant",
        "country": "US",
        "region": "US Gulf",
        "status": "operating",
        "verified": False,
        "verified_on": None,
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(f"%({k})s" for k in row)
    db.execute(f"INSERT INTO refinery ({cols}) VALUES ({marks})", row)


def test_valid_refinery_accepted(db: Conn) -> None:
    add_refinery(db, verified=True, verified_on="2026-01-01")
    row = db.execute("SELECT aliases, loaded_at IS NOT NULL AS l FROM refinery").fetchone()
    assert row == {"aliases": [], "l": True}


@pytest.mark.parametrize(
    "bad",
    [
        {"region": "Mars"},
        {"status": "idle"},
        {"id": "R12"},
        {"id": "r0001"},
        {"verified": False, "verified_on": "2026-01-01"},
        {"name": ""},
        {"nameplate_kbd": 0},
    ],
)
def test_refinery_checks(db: Conn, bad: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        add_refinery(db, **bad)


def test_unit_code_check_and_uniqueness(db: Conn) -> None:
    add_refinery(db)
    db.execute("INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9001', 'CDU')")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9001', 'XYZ')")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9001', 'UTIL')")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute("INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9001', 'CDU')")
    db.execute(
        "INSERT INTO refinery_unit (refinery_id, unit_code, unit_tag) VALUES ('R9001', 'CDU', '2')"
    )


def test_unit_requires_refinery(db: Conn) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        db.execute("INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9999', 'CDU')")


def test_unit_codes_match_unit_product_map(db: Conn) -> None:
    with (ROOT / "data" / "unit_product_map.csv").open(encoding="utf-8", newline="") as f:
        codes = {r["unit_code"] for r in csv.DictReader(f)} - {"UTIL"}
    add_refinery(db)
    for code in sorted(codes):
        with db.transaction():
            db.execute(
                "INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9001', %s)", (code,)
            )
    count = db.execute("SELECT count(*) AS n FROM refinery_unit").fetchone()
    assert count == {"n": len(codes)}
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("INSERT INTO refinery_unit (refinery_id, unit_code) VALUES ('R9001', 'ZZZ')")


def test_status_change_must_differ(db: Conn) -> None:
    add_refinery(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "INSERT INTO refinery_status_change (refinery_id, old_status, new_status)"
            " VALUES ('R9001', 'operating', 'operating')"
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"tier": 4},
        {"access_method": "fax"},
        {"id": "Bad Id"},
        {"fetch_interval_min": 0},
    ],
)
def test_source_checks(db: Conn, bad: dict[str, Any]) -> None:
    row: dict[str, Any] = {
        "id": "s1",
        "name": "S",
        "tier": 1,
        "region_focus": "x",
        "access_method": "rss",
        "feed_url": "https://example.test/feed",
        "article_url_pattern": "https://example.test/*",
        "robots_status": "ok",
        "fetch_interval_min": 60,
    }
    row.update(bad)
    cols = ", ".join(row)
    marks = ", ".join(f"%({k})s" for k in row)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(f"INSERT INTO source ({cols}) VALUES ({marks})", row)


def test_app_grants_match_table(privileges: Callable[[str], set[str]]) -> None:
    for table, expected in GRANTS.items():
        assert privileges(table) == expected, table


def test_app_has_no_privilege_on_alembic_version(privileges: Callable[[str], set[str]]) -> None:
    assert privileges("alembic_version") == set()


def test_public_has_no_privilege(db: Conn) -> None:
    row = db.execute("SELECT has_table_privilege('public', 'refinery', 'SELECT') AS h").fetchone()
    assert row == {"h": False}


def test_app_cannot_write_reference_tables(app_db: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("INSERT INTO source (id) VALUES ('x')")
