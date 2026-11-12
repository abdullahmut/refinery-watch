import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from tests.db.flare_helpers import add_day, add_footprint
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import add_document, insert_raw_outage, insert_raw_restart, unit_id

from refinery_watch.db.connect import Conn
from refinery_watch.flare.support import confidence_as_of, evidence_as_of

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

START = date(2026, 10, 10)
OUTAGE_KNOWN = datetime.now(UTC) - timedelta(days=2)
LATER = datetime.now(UTC) + timedelta(hours=1)
_docs = itertools.count(300)


def source(conn: Conn, event_id: int, kind: str = "other_media", role: str = "primary") -> None:
    known = conn.execute("SELECT known_at FROM outage_event WHERE id = %s", (event_id,))
    row = known.fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence,"
        " known_at) VALUES (%s, %s, %s, %s, 'quoted text', %s)",
        (event_id, add_document(conn, next(_docs)), role, kind, row["known_at"]),
    )


def lift_candidate(conn: Conn, kind: str = "other_media", **over: Any) -> int:
    """A row that meets every condition except the flare day."""
    fields: dict[str, Any] = {
        "confidence": "low",
        "match_score": 92,
        "capacity_basis": "unit_reference",
        "start_date": START,
        "known_at": OUTAGE_KNOWN,
    }
    fields.update(over)
    event = insert_raw_outage(conn, **fields)
    source(conn, event, kind)
    return event


def restart(conn: Conn, ends: int, **over: Any) -> int:
    fields: dict[str, Any] = {
        "confidence": "low",
        "match_score": 92,
        "capacity_basis": "unit_reference",
        "capacity_affected_kbd": 50,
        "start_date": START + timedelta(days=1),
        "known_at": OUTAGE_KNOWN + timedelta(hours=2),
    }
    fields.update(over)
    event = insert_raw_restart(conn, ends, **fields)
    source(conn, event)
    return event


def flagged_day(conn: Conn, day: date, z: float = 4.0, n: int = 2) -> tuple[int, int]:
    fp = conn.execute("SELECT id FROM plant_footprint WHERE refinery_id = 'R9001'").fetchone()
    footprint = int(fp["id"]) if fp else add_footprint(conn)
    return footprint, add_day(conn, footprint, day, z=z, n_detections=n, frp_sum_mw=20)


def effective(conn: Conn, event: int, as_of: datetime = LATER) -> str:
    found = {c.outage_event_id: c for c in confidence_as_of(conn, as_of)}
    return found[event].effective


def test_a_low_row_with_a_flagged_day_inside_the_window_becomes_medium(db: Conn) -> None:
    event = lift_candidate(db)
    _, day = flagged_day(db, date(2026, 10, 12))
    found = {c.outage_event_id: c for c in confidence_as_of(db, LATER)}
    assert (found[event].stored, found[event].effective) == ("low", "medium")
    assert found[event].flare_day_id == day


def test_before_the_flare_row_was_known_it_is_still_low(db: Conn) -> None:
    event = lift_candidate(db)
    flagged_day(db, date(2026, 10, 12))
    known = db.execute("SELECT known_at FROM flare_day").fetchone()
    assert known is not None
    assert effective(db, event, known["known_at"] - timedelta(microseconds=1)) == "low"
    assert effective(db, event, known["known_at"]) == "medium"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 14), "low"),
        (date(2026, 10, 13), "medium"),
        (date(2026, 10, 7), "medium"),
        (date(2026, 10, 6), "low"),
        (date(2026, 10, 10), "medium"),
    ],
)
def test_the_window_is_three_days_either_side(db: Conn, day: date, expected: str) -> None:
    event = lift_candidate(db)
    flagged_day(db, day)
    assert effective(db, event) == expected


def test_the_flagged_day_closest_to_the_start_is_named(db: Conn) -> None:
    event = lift_candidate(db)
    fp, _far = flagged_day(db, date(2026, 10, 13))
    near = add_day(db, fp, date(2026, 10, 11), z=4.0, n_detections=2)
    found = {c.outage_event_id: c for c in confidence_as_of(db, LATER)}
    assert found[event].flare_day_id == near


def test_a_tie_between_two_days_goes_to_the_earlier_day(db: Conn) -> None:
    event = lift_candidate(db)
    fp, before = flagged_day(db, date(2026, 10, 8))
    add_day(db, fp, date(2026, 10, 12), z=4.0, n_detections=2)
    found = {c.outage_event_id: c for c in confidence_as_of(db, LATER)}
    assert found[event].flare_day_id == before


@pytest.mark.parametrize(
    "over",
    [
        {"match_score": 89},
        {"refinery_unit_id": None, "whole_plant": False},
        {"capacity_basis": "not_stated", "capacity_affected_kbd": None},
        {"start_unknown": True},
        {"old_start": True},
        {"event_type": "planned", "match_score": 89},
    ],
    ids=["score", "no_unit", "no_capacity", "start_unknown", "old_start", "planned_low_score"],
)
def test_each_missing_condition_alone_keeps_the_row_low(db: Conn, over: dict[str, Any]) -> None:
    event = lift_candidate(db, **over)
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, event) == "low"


def test_a_planned_row_that_meets_every_condition_is_lifted(db: Conn) -> None:
    event = lift_candidate(db, event_type="planned")
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, event) == "medium"


def test_a_whole_plant_row_counts_as_naming_a_unit(db: Conn) -> None:
    event = lift_candidate(db, refinery_unit_id=None, whole_plant=True)
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, event) == "medium"


def test_a_regulator_only_chain_is_not_lifted(db: Conn) -> None:
    event = lift_candidate(db, kind="regulator")
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, event) == "low"


def test_a_news_source_on_a_later_revision_of_the_chain_counts(db: Conn) -> None:
    root = lift_candidate(db, kind="regulator")
    chain = db.execute("SELECT chain_id FROM outage_event WHERE id = %s", (root,)).fetchone()
    assert chain is not None
    revision = insert_raw_outage(
        db,
        supersedes=root,
        chain_id=chain["chain_id"],
        confidence="low",
        match_score=92,
        capacity_basis="unit_reference",
        start_date=START,
        known_at=OUTAGE_KNOWN + timedelta(hours=1),
    )
    source(db, revision, "other_media")
    flagged_day(db, date(2026, 10, 11))
    found = {c.outage_event_id: c for c in confidence_as_of(db, LATER)}
    assert root not in found
    assert found[revision].effective == "medium"


def test_a_source_known_after_the_time_does_not_count(db: Conn) -> None:
    event = lift_candidate(db, kind="regulator")
    db.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence,"
        " known_at) VALUES (%s, %s, 'corroboration', 'other_media', 'text', %s)",
        (event, add_document(db, next(_docs)), LATER + timedelta(hours=1)),
    )
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, event, LATER) == "low"
    assert effective(db, event, LATER + timedelta(hours=2)) == "medium"


def test_a_restart_row_is_never_lifted(db: Conn) -> None:
    opened = lift_candidate(db, confidence="medium")
    ended = restart(db, opened)
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, ended) == "low"


def test_a_day_below_the_signal_threshold_does_not_lift(db: Conn) -> None:
    event = lift_candidate(db)
    flagged_day(db, date(2026, 10, 11), z=3.4)
    assert effective(db, event) == "low"


def test_a_day_without_a_score_does_not_lift(db: Conn) -> None:
    event = lift_candidate(db)
    fp = add_footprint(db)
    add_day(db, fp, date(2026, 10, 11), baseline_days=10, z=None, n_detections=3)
    assert effective(db, event) == "low"


def test_a_calibrated_threshold_known_before_the_time_replaces_the_default(db: Conn) -> None:
    event = lift_candidate(db)
    flagged_day(db, date(2026, 10, 11), z=4.0)
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('signal', 5.0, 1, 'calibrated')"
    )
    stamp = db.execute(
        "SELECT known_at FROM flare_threshold WHERE method = 'calibrated'"
    ).fetchone()
    assert stamp is not None
    assert effective(db, event, LATER) == "low"
    assert effective(db, event, stamp["known_at"] - timedelta(microseconds=1)) == "medium"


def test_a_threshold_minimum_of_detections_is_respected(db: Conn) -> None:
    event = lift_candidate(db)
    flagged_day(db, date(2026, 10, 11), z=4.0, n=2)
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('signal', 3.5, 3, 'calibrated')"
    )
    assert effective(db, event) == "low"


def test_the_alert_threshold_does_not_change_the_signal_one(db: Conn) -> None:
    event = lift_candidate(db)
    flagged_day(db, date(2026, 10, 11), z=4.0)
    db.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('alert', 9.0, 5, 'calibrated')"
    )
    assert effective(db, event) == "medium"


def test_a_flagged_day_revised_to_unflagged_stops_lifting_from_then_on(db: Conn) -> None:
    event = lift_candidate(db)
    fp, first = flagged_day(db, date(2026, 10, 11))
    add_day(db, fp, date(2026, 10, 11), supersedes=first, z=0.5, n_detections=0)
    revised = db.execute("SELECT known_at FROM flare_day WHERE supersedes = %s", (first,))
    stamp = revised.fetchone()
    assert stamp is not None
    assert effective(db, event, stamp["known_at"] - timedelta(microseconds=1)) == "medium"
    assert effective(db, event, LATER) == "low"


def test_medium_and_high_rows_keep_their_stored_value(db: Conn) -> None:
    medium = lift_candidate(db, confidence="medium")
    high = lift_candidate(db, confidence="high", refinery_unit_id=unit_id(db, "R9001", "VDU"))
    flagged_day(db, date(2026, 10, 11))
    assert effective(db, medium) == "medium"
    assert effective(db, high) == "high"
    found = {c.outage_event_id: c for c in confidence_as_of(db, LATER)}
    assert found[medium].flare_day_id is None
    assert found[high].flare_day_id is None


def test_a_superseded_row_never_appears_and_its_revision_does(db: Conn) -> None:
    root = lift_candidate(db)
    chain = db.execute("SELECT chain_id FROM outage_event WHERE id = %s", (root,)).fetchone()
    assert chain is not None
    revision = insert_raw_outage(
        db,
        supersedes=root,
        chain_id=chain["chain_id"],
        confidence="low",
        match_score=95,
        capacity_basis="unit_reference",
        start_date=START,
        known_at=OUTAGE_KNOWN + timedelta(hours=1),
    )
    source(db, revision)
    seen = {c.outage_event_id for c in confidence_as_of(db, LATER)}
    assert root not in seen
    assert revision in seen
    early = {c.outage_event_id for c in confidence_as_of(db, OUTAGE_KNOWN + timedelta(minutes=1))}
    assert root in early
    assert revision not in early


def test_nothing_is_stored_on_the_outage_rows(db: Conn) -> None:
    lift_candidate(db)
    lift_candidate(db, confidence="high", refinery_unit_id=unit_id(db, "R9001", "VDU"))
    flagged_day(db, date(2026, 10, 11))
    before = db.execute("SELECT * FROM outage_event ORDER BY id").fetchall()
    sources_before = db.execute("SELECT * FROM event_source ORDER BY id").fetchall()
    confidence_as_of(db, LATER)
    evidence_as_of(db, "R9001", date(2026, 10, 11), LATER)
    assert db.execute("SELECT * FROM outage_event ORDER BY id").fetchall() == before
    assert db.execute("SELECT * FROM event_source ORDER BY id").fetchall() == sources_before


def test_the_functions_refuse_a_naive_time(db: Conn) -> None:
    naive = datetime(2026, 11, 12)  # noqa: DTZ001
    with pytest.raises(ValueError, match="aware"):
        confidence_as_of(db, naive)
    with pytest.raises(ValueError, match="aware"):
        evidence_as_of(db, "R9001", START, naive)


def insert_tceq(
    conn: Conn, incident: str, started: datetime | None, refinery: str = "R9001"
) -> int:
    row = conn.execute(
        "INSERT INTO tceq_event (incident_id, report_stage, raw_document_id, refinery_id,"
        " regulated_entity, event_kind, started_at, units_text, report_url)"
        " VALUES (%s, 'initial', %s, %s, 'Plant', 'Emissions event', %s, 'flare', 'https://x.test')"
        " RETURNING id",
        (incident, add_document(conn, next(_docs)), refinery, started),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_evidence_lists_near_open_and_regulator_items_in_order(db: Conn) -> None:
    near = lift_candidate(db, start_date=date(2026, 10, 12), unit_label="CDU 1")
    open_old = lift_candidate(
        db, start_date=date(2026, 9, 1), refinery_unit_id=unit_id(db, "R9001", "VDU")
    )
    closed = lift_candidate(
        db, start_date=date(2026, 8, 1), refinery_unit_id=unit_id(db, "R9001", "FCC")
    )
    restart(db, closed, start_date=date(2026, 8, 5), known_at=OUTAGE_KNOWN + timedelta(hours=3))
    regulator = insert_tceq(db, "T1", datetime(2026, 10, 11, 5, tzinfo=UTC))
    insert_tceq(db, "T2", datetime(2026, 10, 20, 5, tzinfo=UTC))
    insert_tceq(db, "T3", datetime(2026, 10, 11, 5, tzinfo=UTC), refinery="R9003")
    flagged_day(db, date(2026, 10, 12))
    items = evidence_as_of(db, "R9001", date(2026, 10, 12), LATER)
    assert [(i.kind, i.ref_id) for i in items] == [
        ("outage_near", near),
        ("outage_open", open_old),
        ("regulator_near", regulator),
    ]
    assert items[0].event_type == "unplanned"
    assert items[0].unit_label == "CDU 1"
    assert items[0].item_date == date(2026, 10, 12)
    assert items[0].confidence == "medium"
    assert items[1].confidence == "low"
    assert items[2].confidence is None
    assert items[2].unit_label == "flare"


def test_evidence_window_is_plus_or_minus_three_days(db: Conn) -> None:
    inside = lift_candidate(db, start_date=date(2026, 10, 9))
    lift_candidate(db, start_date=date(2026, 10, 8), refinery_unit_id=unit_id(db, "R9001", "VDU"))
    items = evidence_as_of(db, "R9001", date(2026, 10, 12), LATER)
    kinds = {i.ref_id: i.kind for i in items}
    assert kinds[inside] == "outage_near"
    assert sorted(kinds.values()) == ["outage_near", "outage_open"]


def test_evidence_only_holds_what_was_known(db: Conn) -> None:
    lift_candidate(db)
    assert evidence_as_of(db, "R9001", START, OUTAGE_KNOWN - timedelta(hours=1)) == []
    insert_tceq(db, "T9", datetime(2026, 10, 10, tzinfo=UTC))
    stamp = db.execute("SELECT known_at FROM tceq_event").fetchone()
    assert stamp is not None
    before = evidence_as_of(db, "R9001", START, stamp["known_at"] - timedelta(microseconds=1))
    assert [i.kind for i in before] == ["outage_near"]
    after = evidence_as_of(db, "R9001", START, stamp["known_at"])
    assert [i.kind for i in after] == ["outage_near", "regulator_near"]


def test_a_regulator_item_without_a_start_uses_when_it_was_known(db: Conn) -> None:
    ref = insert_tceq(db, "T7", None)
    today = datetime.now(UTC).date()
    items = evidence_as_of(db, "R9001", today, LATER)
    assert [(i.kind, i.ref_id) for i in items] == [("regulator_near", ref)]


def test_a_closed_outage_is_listed_only_when_near(db: Conn) -> None:
    closed = lift_candidate(db, start_date=date(2026, 8, 1))
    restart(db, closed, start_date=date(2026, 8, 5), known_at=OUTAGE_KNOWN + timedelta(hours=3))
    assert evidence_as_of(db, "R9001", date(2026, 10, 12), LATER) == []
    near = evidence_as_of(db, "R9001", date(2026, 8, 3), LATER)
    assert [i.kind for i in near] == ["outage_near", "outage_near"]
    assert {i.event_type for i in near} == {"unplanned", "restart"}
