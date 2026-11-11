"""A restart ends an outage of its own kind: whole plant with whole plant, else by unit."""

from typing import Any

import psycopg
import pytest
from tests.db.helpers import (
    current_ids,
    day,
    insert_restart,
    ts,
    unit_id,
    write,
    write_restart,
)

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import match_restart, restart_duplicate_of

pytestmark = pytest.mark.integration

PLANT: dict[str, Any] = {"refinery_unit_id": None, "whole_plant": True}
UNNAMED: dict[str, Any] = {"refinery_unit_id": None, "whole_plant": False}


def refused_restart(db: Conn, state: str, **over: Any) -> None:
    with pytest.raises(psycopg.Error) as info, db.transaction():
        insert_restart(db, **over)
    assert info.value.sqlstate == state


def test_unnamed_unit_restart_never_ends_a_whole_plant_outage(db: Conn) -> None:
    plant = write(db, known_at=ts(1), **PLANT)
    refused_restart(db, "RW005", known_at=ts(3), start_date=day(3), unit_label="FCC", **UNNAMED)
    assert current_ids(db, ts(4)) == [plant]


def test_named_unit_restart_never_ends_a_whole_plant_outage(db: Conn) -> None:
    plant = write(db, known_at=ts(1), **PLANT)
    refused_restart(db, "RW005", known_at=ts(3), start_date=day(3))
    assert current_ids(db, ts(4)) == [plant]


def test_whole_plant_restart_never_ends_an_unnamed_unit_outage(db: Conn) -> None:
    unnamed = write(db, known_at=ts(1), **UNNAMED)
    refused_restart(db, "RW005", known_at=ts(3), start_date=day(3), **PLANT)
    assert current_ids(db, ts(4)) == [unnamed]


def test_each_kind_ends_its_own_kind(db: Conn) -> None:
    write(db, known_at=ts(1), **UNNAMED)
    write_restart(db, known_at=ts(3), start_date=day(3), **UNNAMED)
    write(db, known_at=ts(4), **PLANT)
    write_restart(db, known_at=ts(6), start_date=day(6), **PLANT)
    assert current_ids(db, ts(7)) == []


def test_match_restart_function_takes_the_whole_plant_flag(db: Conn) -> None:
    plant = write(db, known_at=ts(1), **PLANT)
    assert match_restart(db, "R9001", None, day(2), ts(2), whole_plant=True) == plant
    assert match_restart(db, "R9001", None, day(2), ts(2), whole_plant=False) is None
    assert match_restart(db, "R9001", None, day(2), ts(2)) is None


def test_restart_duplicate_of_separates_plant_and_unit_kinds(db: Conn) -> None:
    write(db, known_at=ts(1), start_date=day(1))
    x = write_restart(db, known_at=ts(2), start_date=day(2))
    unit = unit_id(db)
    assert restart_duplicate_of(db, "R9001", unit, day(3), ts(3)) == x
    assert restart_duplicate_of(db, "R9001", None, day(3), ts(3), whole_plant=True) is None


def test_revision_cannot_flip_a_closed_event_between_plant_and_unnamed(db: Conn) -> None:
    plant = write(db, known_at=ts(1), **PLANT)
    write_restart(db, known_at=ts(3), start_date=day(3), **PLANT)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        write(db, supersedes=plant, known_at=ts(4), **UNNAMED)
    assert info.value.sqlstate == "RW007"


def test_restart_revision_cannot_flip_between_plant_and_unnamed(db: Conn) -> None:
    write(db, known_at=ts(1), **PLANT)
    x = write_restart(db, known_at=ts(3), start_date=day(3), **PLANT)
    refused_restart(db, "RW007", supersedes=x, known_at=ts(4), start_date=day(3), **UNNAMED)
