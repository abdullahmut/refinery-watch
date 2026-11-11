from datetime import datetime

import pytest
from tests.db.helpers import (
    day,
    insert_raw_outage,
    insert_raw_restart,
    sourced,
    ts,
    unit_id,
    write,
    write_restart,
)

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
        " 'restart_duplicate_of(text, bigint, date, timestamptz, boolean)', 'EXECUTE') AS p"
    ).fetchone()
    assert row == {"p": False}


def test_late_report_of_a_closed_outage_is_the_same_event(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    assert outage_dup(db, 1, ts(7)) == [e1]
    assert outage_dup(db, 3, ts(7)) == [e1]


def test_report_on_or_after_the_restart_date_is_a_new_event(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    assert outage_dup(db, 5, ts(7)) == []
    assert outage_dup(db, 6, ts(7)) == []


def test_closed_chain_outside_the_window_is_not_a_match(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(9), start_date=day(9))
    assert outage_dup(db, 8, ts(10)) == []


def test_closure_not_yet_known_leaves_the_chain_open_for_matching(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    assert outage_dup(db, 1, ts(4)) == [e1]


def raw_outage(db: Conn, known: datetime, start: int) -> int:
    return sourced(db, insert_raw_outage(db, known_at=known, start_date=day(start)))


def raw_restart(db: Conn, ends: int, known: datetime, start: int) -> int:
    return sourced(db, insert_raw_restart(db, ends, known_at=known, start_date=day(start)))


def test_restart_duplicate_tie_on_start_goes_to_the_later_known_row(db: Conn) -> None:
    first = raw_outage(db, ts(1), 3)
    second = raw_outage(db, ts(2), 3)
    raw_restart(db, first, ts(4), 4)
    x_second = raw_restart(db, second, ts(5), 5)
    assert restart_dup(db, 6, ts(6)) == x_second


def test_restart_duplicate_tie_on_start_and_known_goes_to_the_lower_id(db: Conn) -> None:
    first = raw_outage(db, ts(2), 3)
    second = raw_outage(db, ts(2), 3)
    x_first = raw_restart(db, first, ts(4), 4)
    raw_restart(db, second, ts(5), 5)
    assert first < second
    assert restart_dup(db, 6, ts(6)) == x_first


def test_restart_duplicate_follows_a_restart_that_ends_a_revised_row(db: Conn) -> None:
    root = write(db, known_at=ts(1), start_date=day(1))
    revised = write(db, supersedes=root, known_at=ts(2), start_date=day(2))
    x = write_restart(db, known_at=ts(5), start_date=day(5))
    assert db.execute("SELECT ends_event_id FROM outage_event WHERE id = %s", (x,)).fetchone() == {
        "ends_event_id": revised
    }
    assert restart_dup(db, 6, ts(6)) == x


def test_restart_duplicate_follows_a_restart_that_ends_the_root_after_a_later_revision(
    db: Conn,
) -> None:
    root = write(db, known_at=ts(1), start_date=day(1))
    x = write_restart(db, known_at=ts(5), start_date=day(5))
    write(db, supersedes=root, known_at=ts(7), start_date=day(2))
    assert restart_dup(db, 8, ts(8)) == x
