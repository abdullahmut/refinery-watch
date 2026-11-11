import threading
import time
from typing import Any

import psycopg
import pytest
from tests.db.helpers import (
    add_document,
    add_source,
    assert_unit_exclusive,
    day,
    insert_outage,
    open_in_schema,
    ts,
    unit_id,
    write,
    write_restart,
)

from refinery_watch.db.connect import Conn

pytestmark = pytest.mark.integration


def refused(db: Conn, **over: Any) -> None:
    with pytest.raises(psycopg.Error) as info, db.transaction():
        insert_outage(db, **over)
    assert info.value.sqlstate == "RW006"


def test_second_outage_on_an_open_unit_is_refused(db: Conn) -> None:
    write(db, known_at=ts(1))
    refused(db, known_at=ts(20), start_date=day(20))
    refused(db, known_at=ts(21), start_date=day(21), event_type="planned")


def test_other_units_and_plants_are_free(db: Conn) -> None:
    write(db, known_at=ts(1))
    write(db, known_at=ts(2), refinery_unit_id=unit_id(db, code="VDU"))
    write(db, known_at=ts(3), refinery_id="R9003", refinery_unit_id=unit_id(db, "R9003", "CDU"))


def test_unit_is_free_again_once_the_restart_is_known(db: Conn) -> None:
    write(db, known_at=ts(1))
    write_restart(db, known_at=ts(5), start_date=day(5))
    write(db, known_at=ts(20), start_date=day(20))


def test_revision_of_the_open_outage_is_not_a_second_outage(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    write(db, supersedes=e1, known_at=ts(2), start_date=day(2))


def test_whole_plant_and_unnamed_share_one_slot(db: Conn) -> None:
    write(db, known_at=ts(1), refinery_unit_id=None, whole_plant=True)
    refused(db, known_at=ts(2), refinery_unit_id=None, whole_plant=False)
    refused(db, known_at=ts(3), refinery_unit_id=None, whole_plant=True)


def test_future_planned_turnaround_holds_the_unit(db: Conn) -> None:
    write(db, known_at=ts(1), event_type="planned", start_date=day(28))
    refused(db, known_at=ts(2), start_date=day(2))


def test_unit_is_exclusive_across_a_busy_history(db: Conn) -> None:
    e1 = write(db, known_at=ts(1))
    refused(db, known_at=ts(2), start_date=day(2))
    write(db, supersedes=e1, known_at=ts(3), start_date=day(3))
    write_restart(db, known_at=ts(5), start_date=day(5))
    write(db, known_at=ts(6), start_date=day(6))
    refused(db, known_at=ts(7), start_date=day(7))
    assert_unit_exclusive(db, [ts(d) for d in range(1, 10)])


def race(
    seeded_schema: str, app_url: str, refinery_id: str, code: str, begin_first: bool
) -> tuple[str | None, bool]:
    """A inserts and holds. B inserts on the same unit and must wait. A commits."""
    a = open_in_schema(app_url, seeded_schema)
    b = open_in_schema(app_url, seeded_schema)
    outcome: list[str | None] = []

    def second_writer() -> None:
        try:
            insert_outage(
                b,
                known_at=None,
                refinery_id=refinery_id,
                refinery_unit_id=unit_id(b, refinery_id, code),
            )
            b.commit()
            outcome.append(None)
        except psycopg.Error as exc:
            b.rollback()
            outcome.append(exc.sqlstate)

    try:
        if begin_first:
            b.execute("SELECT count(*) FROM outage_event")
        event = insert_outage(
            a,
            known_at=None,
            refinery_id=refinery_id,
            refinery_unit_id=unit_id(a, refinery_id, code),
        )
        add_source(a, event, add_document(a, 1))
        thread = threading.Thread(target=second_writer)
        thread.start()
        time.sleep(0.8)
        blocked = thread.is_alive()
        a.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        a.close()
        b.close()
    return outcome[0], blocked


@pytest.mark.parametrize("begin_first", [False, True])
def test_concurrent_writer_waits_then_fails_with_rw006(
    seeded_schema: str, app_url: str, begin_first: bool
) -> None:
    started = time.monotonic()
    code, blocked = race(
        seeded_schema,
        app_url,
        "R9001" if not begin_first else "R9003",
        "CDU",
        begin_first,
    )
    assert blocked
    assert code == "RW006"
    assert time.monotonic() - started < 10
