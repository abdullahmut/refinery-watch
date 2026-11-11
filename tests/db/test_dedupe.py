from datetime import datetime

import pytest
from tests.db.helpers import day, ts, unit_id, write, write_restart

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration


def outage_dup(
    conn: Conn,
    start: int,
    as_of: datetime,
    unit: int | None = -1,
    whole_plant: bool = False,
    start_unknown: bool = False,
) -> list[int]:
    value = unit_id(conn) if unit == -1 else unit
    rows = conn.execute(
        "SELECT c FROM outage_duplicate_of('R9001', %s, %s, %s, %s, %s) AS c",
        (value, whole_plant, day(start), start_unknown, as_of),
    ).fetchall()
    return [int(r["c"]) for r in rows]


def restart_dup(conn: Conn, start: int, as_of: datetime, unit: int | None = -1) -> int | None:
    value = unit_id(conn) if unit == -1 else unit
    row = conn.execute(
        "SELECT restart_duplicate_of('R9001', %s, %s, %s) AS c", (value, day(start), as_of)
    ).fetchone()
    assert row is not None
    return None if row["c"] is None else int(row["c"])


def test_within_three_days_of_an_open_chain(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(10))
    for start in (7, 10, 13):
        assert outage_dup(db, start, ts(2)) == [e1]


def test_four_days_apart_is_a_new_event(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(10))
    assert outage_dup(db, 6, ts(2)) == []
    assert outage_dup(db, 14, ts(2)) == []


def test_revision_chain_is_reported_by_its_chain_id(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(10))
    write(db, supersedes=e1, known_at=ts(2), start_date=day(11))
    assert outage_dup(db, 11, ts(3)) == [e1]


def test_closed_chain_is_not_searched(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(2), start_date=day(2))
    assert outage_dup(db, 3, ts(3)) == []


def test_planned_and_unplanned_are_one_event(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(1))
    assert outage_dup(db, 1, ts(2)) == [e1]


def test_whole_plant_only_matches_whole_plant(db: Conn) -> None:
    plant = write(db, known_at=ts(1), start_date=day(2), refinery_unit_id=None, whole_plant=True)
    assert outage_dup(db, 1, ts(3), unit=None, whole_plant=True) == [plant]
    assert outage_dup(db, 30, ts(3), unit=None, whole_plant=True) == []


def test_whole_plant_row_does_not_match_a_cdu_report(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1), refinery_unit_id=None, whole_plant=True)
    assert outage_dup(db, 2, ts(2)) == []
    assert outage_dup(db, 2, ts(2), unit=None) == []


def test_cdu_row_does_not_match_a_whole_plant_report(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    assert outage_dup(db, 2, ts(2), unit=None, whole_plant=True) == []


def test_null_unit_report_matches_an_open_cdu_chain(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(1))
    assert outage_dup(db, 2, ts(2), unit=None) == [e1]


def test_two_open_units_and_a_null_unit_report_return_two_chains(db: Conn) -> None:
    a = write(db, known_at=ts(1), start_date=day(1))
    b = write(db, known_at=ts(2), start_date=day(2), refinery_unit_id=unit_id(db, code="VDU"))
    assert outage_dup(db, 2, ts(3), unit=None) == sorted([a, b])


def test_named_unit_report_ignores_another_named_unit(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    assert outage_dup(db, 1, ts(2), unit=unit_id(db, code="VDU")) == []


def test_start_unknown_waives_the_window(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(1))
    assert outage_dup(db, 20, ts(2)) == []
    assert outage_dup(db, 20, ts(2), start_unknown=True) == [e1]


def test_start_unknown_on_the_stored_row_waives_the_window(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(20), start_unknown=True, confidence="low")
    assert outage_dup(db, 1, ts(2)) == [e1]


def test_dedupe_only_sees_what_was_known(db: Conn) -> None:
    write(db, known_at=ts(5), start_date=day(5))
    assert outage_dup(db, 5, ts(4)) == []


def test_second_report_of_a_restart_returns_the_restart_chain(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    x1 = write_restart(db, known_at=ts(5), start_date=day(5))
    assert restart_dup(db, 5, ts(6)) == x1
    assert restart_dup(db, 9, ts(6)) == x1


def test_restart_of_a_later_outage_is_not_a_duplicate(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    write(db, known_at=ts(11), start_date=day(11))
    assert restart_dup(db, 12, ts(12)) is None


def test_restart_with_nothing_to_end_is_not_a_duplicate(db: Conn) -> None:
    assert restart_dup(db, 5, ts(6)) is None
    write(db, known_at=ts(7), start_date=day(7))
    assert restart_dup(db, 9, ts(8)) is None


def test_restart_before_the_outage_started_finds_nothing(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(10))
    write_restart(db, known_at=ts(11), start_date=day(11))
    assert restart_dup(db, 5, ts(12)) is None


def test_app_role_runs_the_lookups_and_public_cannot(db: Conn, app_db: Conn) -> None:
    assert outage_dup(app_db, 1, ts(1)) == []
    row = db.execute(
        "SELECT has_function_privilege('public',"
        " 'restart_duplicate_of(text, bigint, date, timestamptz)', 'EXECUTE') AS p"
    ).fetchone()
    assert row == {"p": False}
