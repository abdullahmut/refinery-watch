"""The definitions rule repros, run against Postgres as the app role.

Days keep their repro numbers and move back one year, because known_at may not be in the future.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.db.helpers import add_document, assert_unit_exclusive, day, ts, unit_id

from refinery_watch.dates import resolve_weekday
from refinery_watch.db.connect import Conn
from refinery_watch.db.events import (
    NewEventRow,
    Review,
    SourceLink,
    Written,
    outage_as_of,
    outage_duplicate_of,
    restart_duplicate_of,
    write_event,
)

pytestmark = pytest.mark.integration

R = "R9001"


def outage(conn: Conn, d: int, known: int, unit: bool = True, **over: Any) -> NewEventRow:
    fields: dict[str, Any] = {
        "event_type": "unplanned",
        "refinery_id": R,
        "refinery_unit_id": unit_id(conn) if unit else None,
        "whole_plant": not unit,
        "start_date": day(d),
        "confidence": "high",
        "capacity_basis": "stated",
        "match_score": 100,
        "capacity_affected_kbd": Decimal(200),
        "known_at": ts(known),
    }
    fields.update(over)
    return NewEventRow(**fields)


def restart(conn: Conn, d: int, known: int, unit: bool = True, **over: Any) -> NewEventRow:
    fields: dict[str, Any] = {
        "event_type": "restart",
        "capacity_basis": "not_stated",
        "capacity_affected_kbd": None,
    }
    fields.update(over)
    return outage(conn, d, known, unit, **fields)


def put(conn: Conn, row: NewEventRow) -> Written | Review:
    count = conn.execute("SELECT count(*) AS n FROM raw_document").fetchone()
    assert count is not None
    link = SourceLink(add_document(conn, int(count["n"]) + 1), "company_statement", "quoted text")
    return write_event(conn, row, link)


def written(conn: Conn, row: NewEventRow) -> Written:
    result = put(conn, row)
    assert isinstance(result, Written), result
    return result


def ids(conn: Conn, as_of: datetime) -> set[int]:
    return {r.id for r in outage_as_of(conn, as_of)}


def publication_date(conn: Conn, published: datetime, tz: str | None) -> date:
    found = conn.execute(
        "INSERT INTO raw_document (source_id, url, content_sha256, object_key, body_chars,"
        " published_at, published_tz, prefilter_passed, prefilter_terms)"
        " VALUES ('fixture_src', 'https://example.test/p', repeat('d', 64), 'raw/p', 1,"
        " %s, %s, true, '{refinery}') RETURNING publication_date",
        (published, tz),
    ).fetchone()
    assert found is not None
    value = found["publication_date"]
    assert isinstance(value, date)
    return value


def test_old_restart_does_not_close_a_later_shutdown(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    x = written(app_db, restart(app_db, 5, 5))
    assert x.ends_event_id == e1.id
    e3 = written(app_db, outage(app_db, 20, 20))
    assert ids(app_db, ts(6)) == set()
    assert ids(app_db, ts(21)) == {e3.id}


def test_shutdown_and_its_own_restart_are_not_one_event(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    assert restart_duplicate_of(app_db, R, unit_id(app_db), day(3), ts(3)) is None
    x = written(app_db, restart(app_db, 3, 3))
    assert x.ends_event_id == e1.id
    chain = app_db.execute(
        "SELECT count(*) AS n FROM outage_event WHERE chain_id = %s", (e1.chain_id,)
    ).fetchone()
    assert chain == {"n": 1}


def test_announced_restart_weekday_is_not_in_the_past(app_db: Conn) -> None:
    thu = publication_date(app_db, datetime(2026, 11, 12, 9, tzinfo=UTC), None)
    assert thu == date(2026, 11, 12)
    assert resolve_weekday(thu, 2, "future") == date(2026, 11, 18)
    assert resolve_weekday(thu, 1, "past") == date(2026, 11, 10)
    assert resolve_weekday(thu, 3, "last") == date(2026, 11, 5)
    assert resolve_weekday(thu, 3, "next") == date(2026, 11, 19)


def test_revision_after_restart_keeps_event_closed(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    written(app_db, restart(app_db, 5, 5))
    written(app_db, outage(app_db, 1, 7, supersedes=e1.id))
    assert ids(app_db, ts(8)) == set()


def test_late_reported_restart_ends_the_event_it_follows(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    second = put(app_db, outage(app_db, 20, 20))
    assert isinstance(second, Review)
    assert second.reason == "unit_has_open_outage"
    x = written(app_db, restart(app_db, 10, 25))
    assert x.ends_event_id == e1.id
    assert ids(app_db, ts(26)) == set()
    e3 = written(app_db, outage(app_db, 20, 26))
    assert ids(app_db, ts(27)) == {e3.id}
    assert_unit_exclusive(app_db, [ts(d) for d in range(1, 29)])


def test_revised_restart_keeps_its_event(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    x2 = written(app_db, restart(app_db, 5, 5))
    e3 = written(app_db, outage(app_db, 20, 20))
    x4 = written(app_db, restart(app_db, 4, 22, supersedes=x2.id))
    assert x4.ends_event_id == e1.id
    assert ids(app_db, ts(23)) == {e3.id}


def test_whole_plant_restart_closes_whole_plant_outage(app_db: Conn) -> None:
    written(app_db, outage(app_db, 1, 1, unit=False))
    written(app_db, restart(app_db, 5, 5, unit=False))
    assert ids(app_db, ts(6)) == set()


def test_one_outage_reported_as_planned_and_unplanned_counts_once(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    found = outage_duplicate_of(app_db, R, unit_id(app_db), False, day(1), False, ts(2))
    assert found == [e1.chain_id]


def test_weekday_uses_source_local_publication_date(app_db: Conn) -> None:
    published = datetime(2026, 11, 12, 23, tzinfo=UTC)
    pub = publication_date(app_db, published, "Asia/Singapore")
    assert pub == date(2026, 11, 13)
    assert resolve_weekday(pub, 3, "future") == date(2026, 11, 19)


def test_fork_goes_to_review(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    written(app_db, outage(app_db, 1, 3, supersedes=e1.id))
    result = put(app_db, outage(app_db, 1, 4, supersedes=e1.id))
    assert isinstance(result, Review)
    assert result.reason == "supersedes_not_current"


def test_unit_is_not_counted_offline_twice(app_db: Conn) -> None:
    written(app_db, outage(app_db, 1, 1))
    result = put(app_db, outage(app_db, 20, 20))
    assert isinstance(result, Review)
    assert result.reason == "unit_has_open_outage"
    offline = sum(
        r.capacity_affected_kbd or Decimal(0)
        for r in outage_as_of(app_db, ts(21))
        if r.refinery_unit_id == unit_id(app_db)
    )
    assert offline == 200


def test_restarts_of_two_outages_are_two_restarts(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    x = written(app_db, restart(app_db, 10, 10))
    assert x.ends_event_id == e1.id
    unit = unit_id(app_db)
    assert outage_duplicate_of(app_db, R, unit, False, day(11), False, ts(11)) == []
    e2 = written(app_db, outage(app_db, 11, 11))
    assert restart_duplicate_of(app_db, R, unit, day(12), ts(12)) is None
    x2 = written(app_db, restart(app_db, 12, 12))
    assert x2.ends_event_id == e2.id
    assert x2.chain_id != x.chain_id


def test_retrip_after_restart_is_a_new_outage(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    written(app_db, restart(app_db, 2, 2))
    unit = unit_id(app_db)
    assert outage_duplicate_of(app_db, R, unit, False, day(3), False, ts(3)) == []
    e3 = written(app_db, outage(app_db, 3, 3))
    assert e3.chain_id == e3.id
    assert e3.chain_id != e1.chain_id


def test_restart_example_holds_under_open_decision_6(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    second = put(app_db, outage(app_db, 20, 20))
    assert isinstance(second, Review)
    assert second.reason == "unit_has_open_outage"
    x1 = put(app_db, restart(app_db, 10, 25))
    assert isinstance(x1, Written)
    assert x1.ends_event_id == e1.id
    assert ids(app_db, ts(24)) == {e1.id}
    assert ids(app_db, ts(26)) == set()
    e2 = written(app_db, outage(app_db, 20, 26))
    assert ids(app_db, ts(27)) == {e2.id}
    assert_unit_exclusive(app_db, [ts(d) for d in range(1, 29)])


def test_supersede_branch_sends_late_restart_to_review(app_db: Conn) -> None:
    e1 = written(app_db, outage(app_db, 1, 1))
    written(app_db, outage(app_db, 20, 20, supersedes=e1.id))
    x1 = put(app_db, restart(app_db, 10, 25))
    assert isinstance(x1, Review)
    assert x1.reason == "no_restart_match"
