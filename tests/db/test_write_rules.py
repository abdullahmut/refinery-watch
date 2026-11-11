"""One test per rule of the insert trigger, by SQLSTATE."""

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from tests.db.helpers import (
    current_ids,
    day,
    insert_outage,
    insert_restart,
    ts,
    unit_id,
    write,
    write_restart,
)

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration


def refused(db: Conn, state: str, restart: bool = False, **over: Any) -> None:
    insert = insert_restart if restart else insert_outage
    with pytest.raises(psycopg.Error) as info, db.transaction():
        insert(db, **over)
    assert info.value.sqlstate == state


def column(db: Conn, row_id: int, name: str) -> Any:
    row = db.execute(f"SELECT {name} AS v FROM outage_event WHERE id = %s", (row_id,)).fetchone()
    assert row is not None
    return row["v"]


def test_rw001_repeatable_read_is_refused(db: Conn) -> None:
    db.rollback()
    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    refused(db, "RW001", known_at=ts(1))


def test_rw002_future_known_at(db: Conn) -> None:
    refused(db, "RW002", known_at=datetime(2999, 1, 1, tzinfo=UTC))


def test_rw002_stale_or_equal_known_at(db: Conn) -> None:
    write(db, known_at=ts(5))
    refused(db, "RW002", known_at=ts(4), refinery_unit_id=None, whole_plant=True)
    refused(db, "RW002", known_at=ts(5), refinery_unit_id=None, whole_plant=True)
    write(db, known_at=ts(6), refinery_unit_id=None, whole_plant=True)


def test_omitted_known_at_is_filled_and_later_than_earlier_rows(db: Conn) -> None:
    first = write(db, known_at=ts(1))
    second = write(db, known_at=None, refinery_unit_id=None, whole_plant=True)
    third = write(db, known_at=None, refinery_unit_id=unit_id(db, code="VDU"))
    known = [column(db, i, "known_at") for i in (first, second, third)]
    assert known == sorted(known)
    assert known[1] > ts(1)
    assert known[2] > known[1]


def test_rw003_second_revision_of_one_row(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    write(db, supersedes=e1, known_at=ts(2))
    refused(db, "RW003", supersedes=e1, known_at=ts(3))


def test_rw003_superseding_a_missing_row(db: Conn) -> None:
    refused(db, "RW003", supersedes=999999999, known_at=ts(3))


def test_rw004_family_mismatch(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    refused(db, "RW004", restart=True, supersedes=e1, known_at=ts(2))
    x1 = write_restart(db, known_at=ts(3), start_date=day(3))
    refused(db, "RW004", supersedes=x1, known_at=ts(4))


def test_rw005_restart_without_open_outage(db: Conn) -> None:
    refused(db, "RW005", restart=True, known_at=ts(5), start_date=day(5))


def test_rw005_restart_before_the_outage_started(db: Conn) -> None:
    write(db, known_at=ts(10), start_date=day(8))
    refused(db, "RW005", restart=True, known_at=ts(11), start_date=day(5))


def test_rw006_revision_into_an_occupied_unit(db: Conn) -> None:
    write(db, known_at=ts(1))
    plant = write(db, known_at=ts(2), refinery_unit_id=None, whole_plant=True)
    refused(db, "RW006", supersedes=plant, known_at=ts(3), whole_plant=False)


def test_revision_of_whole_plant_chain_into_a_free_unit(db: Conn) -> None:
    plant = write(db, known_at=ts(1), refinery_unit_id=None, whole_plant=True)
    named = write(db, supersedes=plant, known_at=ts(2), refinery_unit_id=unit_id(db, code="VDU"))
    assert column(db, named, "chain_id") == plant
    assert current_ids(db, ts(3)) == [named]


def test_rw007_revision_cannot_change_refinery(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    refused(
        db,
        "RW007",
        supersedes=e1,
        known_at=ts(2),
        refinery_id="R9003",
        refinery_unit_id=unit_id(db, "R9003", "CDU"),
    )


def test_rw007_closed_event_cannot_change_unit(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    refused(
        db,
        "RW007",
        supersedes=e1,
        known_at=ts(6),
        refinery_unit_id=None,
        whole_plant=True,
    )


def test_rw007_restart_revision_cannot_change_unit(db: Conn) -> None:
    write(db, known_at=ts(1))
    x1 = write_restart(db, known_at=ts(5), start_date=day(5))
    refused(
        db,
        "RW007",
        restart=True,
        supersedes=x1,
        known_at=ts(6),
        start_date=day(5),
        refinery_unit_id=None,
        whole_plant=True,
    )


def test_rw008_restart_revision_before_event_start(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(3))
    x1 = write_restart(db, known_at=ts(5), start_date=day(5))
    refused(db, "RW008", restart=True, supersedes=x1, known_at=ts(6), start_date=day(2))


def test_rw009_restart_naming_the_wrong_event(db: Conn) -> None:
    write(db, known_at=ts(1))
    other = write(db, known_at=ts(2), refinery_unit_id=unit_id(db, code="VDU"))
    refused(db, "RW009", restart=True, known_at=ts(5), start_date=day(5), ends_event_id=other)


def test_rw009_restart_revision_naming_another_event(db: Conn) -> None:
    write(db, known_at=ts(1))
    other = write(db, known_at=ts(2), refinery_unit_id=unit_id(db, code="VDU"))
    x1 = write_restart(db, known_at=ts(5), start_date=day(5))
    refused(
        db,
        "RW009",
        restart=True,
        supersedes=x1,
        known_at=ts(6),
        start_date=day(5),
        ends_event_id=other,
    )


def test_rw009_outage_cannot_end_an_event(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    refused(db, "RW009", known_at=ts(2), refinery_unit_id=None, whole_plant=True, ends_event_id=e1)


def test_rw010_revision_after_restart_date(db: Conn) -> None:
    e1 = write(db, known_at=ts(1), start_date=day(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    refused(db, "RW010", supersedes=e1, known_at=ts(7), start_date=day(8))
    on_the_day = write(db, supersedes=e1, known_at=ts(7), start_date=day(5))
    assert column(db, on_the_day, "chain_id") == e1


def test_rw013_whole_plant_restart_with_units_open(db: Conn) -> None:
    write(db, known_at=ts(1))
    refused(
        db,
        "RW013",
        restart=True,
        known_at=ts(5),
        start_date=day(5),
        refinery_unit_id=None,
        whole_plant=True,
    )


def test_whole_plant_restart_with_nothing_open_is_rw005(db: Conn) -> None:
    refused(
        db,
        "RW005",
        restart=True,
        known_at=ts(5),
        start_date=day(5),
        refinery_unit_id=None,
        whole_plant=True,
    )


def test_rw014_chain_id_supplied(db: Conn) -> None:
    refused(db, "RW014", known_at=ts(1), chain_id=5)


def test_restart_revision_copies_ends_event_id(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    x1 = write_restart(db, known_at=ts(5), start_date=day(5))
    x2 = write_restart(db, supersedes=x1, known_at=ts(6), start_date=day(4))
    assert column(db, x1, "ends_event_id") == e1
    assert column(db, x2, "ends_event_id") == e1
    assert column(db, x2, "chain_id") == x1
    assert current_ids(db, ts(7)) == []


def test_root_rows_start_their_own_chain(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    assert column(db, e1, "chain_id") == e1
    assert column(db, e1, "ends_event_id") is None
    e2 = write(db, supersedes=e1, known_at=ts(2), start_date=day(2))
    assert column(db, e2, "chain_id") == e1
    assert current_ids(db, ts(3)) == [e2]


def test_rw015_restart_dated_after_the_day_it_became_known(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    refused(db, "RW015", restart=True, known_at=ts(5), start_date=day(20))
    assert current_ids(db, ts(10)) == [e1]


def test_restart_dated_the_day_it_became_known_is_accepted(db: Conn) -> None:
    write(db, known_at=ts(1))
    write_restart(db, known_at=ts(5, 0), start_date=day(5))
    assert current_ids(db, ts(6)) == []


def test_rw015_applies_to_restart_revisions(db: Conn) -> None:
    write(db, known_at=ts(1))
    x1 = write_restart(db, known_at=ts(5), start_date=day(5))
    refused(db, "RW015", restart=True, supersedes=x1, known_at=ts(6), start_date=day(9))


def test_outage_dated_in_the_future_is_still_a_planned_turnaround(db: Conn) -> None:
    write(db, known_at=ts(1), event_type="planned", start_date=day(28))


def test_restart_from_an_asia_morning_is_not_in_the_future(db: Conn) -> None:
    """07:30 SGT on 13 Nov is 23:30 UTC on 12 Nov. The article says back today."""
    write(db, known_at=ts(1))
    known = datetime(2025, 11, 12, 23, 30, tzinfo=UTC)
    write_restart(db, known_at=known, start_date=day(13))
    assert current_ids(db, ts(14)) == []


def test_rw015_two_days_ahead_is_still_future(db: Conn) -> None:
    write(db, known_at=ts(1))
    known = datetime(2025, 11, 12, 23, 30, tzinfo=UTC)
    refused(db, "RW015", restart=True, known_at=known, start_date=day(14))
