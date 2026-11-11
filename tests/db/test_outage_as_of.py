from typing import Any

import pytest
from tests.db.helpers import (
    current_ids,
    day,
    insert_raw_outage,
    insert_raw_restart,
    sourced,
    ts,
)

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration


def outage(conn: Conn, **over: Any) -> int:
    return sourced(conn, insert_raw_outage(conn, **over))


def restart(conn: Conn, ends: int, **over: Any) -> int:
    return sourced(conn, insert_raw_restart(conn, ends, **over))


def test_row_is_invisible_before_known_and_visible_at_known(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    assert current_ids(db, ts(1, 5)) == []
    assert current_ids(db, ts(1)) == [e1]


def test_revision_replaces_old_row_from_its_known_at(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    e2 = outage(db, known_at=ts(3), supersedes=e1, chain_id=e1, start_date=day(2))
    assert current_ids(db, ts(2)) == [e1]
    assert current_ids(db, ts(3)) == [e2]
    assert current_ids(db, ts(9)) == [e2]


def test_restart_known_late_does_not_close_before_it_is_known(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    restart(db, e1, known_at=ts(25), start_date=day(10))
    assert current_ids(db, ts(24)) == [e1]
    assert current_ids(db, ts(25)) == []


def test_restart_closes_the_whole_chain(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    restart(db, e1, known_at=ts(5), start_date=day(5))
    outage(db, known_at=ts(7), supersedes=e1, chain_id=e1)
    assert current_ids(db, ts(8)) == []


def test_revision_of_a_restart_keeps_closing(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    x1 = restart(db, e1, known_at=ts(5), start_date=day(5))
    restart(db, e1, known_at=ts(6), start_date=day(4), supersedes=x1, chain_id=x1)
    assert current_ids(db, ts(5)) == []
    assert current_ids(db, ts(7)) == []


def test_restart_rows_are_never_returned(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    restart(db, e1, known_at=ts(2), start_date=day(2))
    assert current_ids(db, ts(3)) == []


def test_planned_not_yet_started_is_returned(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1), event_type="planned", start_date=day(28))
    assert current_ids(db, ts(2)) == [e1]


def test_capacity_edit_after_write_changes_nothing(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1))
    before = db.execute("SELECT * FROM outage_as_of(%s)", (ts(2),)).fetchall()
    db.execute("UPDATE refinery_unit SET capacity_kbd = 999 WHERE refinery_id = 'R9001'")
    db.execute("UPDATE refinery SET nameplate_kbd = 1 WHERE id = 'R9001'")
    after = db.execute("SELECT * FROM outage_as_of(%s)", (ts(2),)).fetchall()
    assert before == after
    assert after[0]["id"] == e1
    assert after[0]["capacity_affected_kbd"] == 200


def match(conn: Conn, start: int, as_of: int = 30, unit: int | None = -1) -> int | None:
    unit_id = conn.execute(
        "SELECT id FROM refinery_unit WHERE refinery_id = 'R9001' AND unit_code = 'CDU'"
    ).fetchone()
    assert unit_id is not None
    value = unit_id["id"] if unit == -1 else unit
    row = conn.execute(
        "SELECT match_restart('R9001', %s, %s, %s) AS m", (value, day(start), ts(as_of))
    ).fetchone()
    assert row is not None
    return None if row["m"] is None else int(row["m"])


def test_match_restart_picks_open_row_on_or_before_the_restart(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1), start_date=day(1))
    assert match(db, 5) == e1
    assert match(db, 1) == e1


def test_match_restart_ignores_later_starts_and_closed_events(db: Conn) -> None:
    e1 = outage(db, known_at=ts(1), start_date=day(1))
    outage(db, known_at=ts(20), start_date=day(20), refinery_unit_id=None)
    assert match(db, 10) == e1
    restart(db, e1, known_at=ts(5), start_date=day(5))
    assert match(db, 10, as_of=6) is None
    assert match(db, 10, as_of=4) == e1


def test_match_restart_unit_key_must_match(db: Conn) -> None:
    outage(db, known_at=ts(1), refinery_unit_id=None)
    assert match(db, 5) is None
    assert match(db, 5, unit=None) is not None


def test_match_restart_tie_breaks_on_known_at_then_id(db: Conn) -> None:
    older = outage(db, known_at=ts(1), start_date=day(3))
    newer = outage(db, known_at=ts(2), start_date=day(3))
    assert match(db, 5) == newer
    tied = outage(db, known_at=ts(2), start_date=day(3))
    assert tied > newer > older
    assert match(db, 5) == newer
    latest_start = outage(db, known_at=ts(1), start_date=day(4))
    assert match(db, 5) == latest_start


def test_app_role_can_execute_and_public_cannot(db: Conn, app_db: Conn) -> None:
    assert app_db.execute("SELECT count(*) AS n FROM outage_as_of(now())").fetchone() == {"n": 0}
    row = db.execute(
        "SELECT has_function_privilege('public', 'outage_as_of(timestamptz)', 'EXECUTE') AS p,"
        " has_function_privilege('refinery_test_app', 'outage_as_of(timestamptz)', 'EXECUTE') AS a,"
        " has_function_privilege('public',"
        " 'match_restart(text, bigint, date, timestamptz, boolean)', 'EXECUTE') AS m"
    ).fetchone()
    assert row == {"p": False, "a": True, "m": False}
