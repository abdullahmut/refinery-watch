"""Event write path and the as-of and dedupe reads. Callers own the transaction."""

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal

import psycopg
from psycopg.rows import DictRow

from refinery_watch.db.connect import Conn

EventType = Literal["unplanned", "planned", "restart"]
Confidence = Literal["high", "medium", "low"]
CapacityBasis = Literal["stated", "unit_reference", "plant_nameplate", "not_stated"]
SourceKind = Literal[
    "company_statement",
    "regulator",
    "named_official",
    "anonymous_sources",
    "other_media",
    "unclear",
]
ReviewReason = Literal[
    "supersedes_not_current",
    "family_mismatch",
    "no_restart_match",
    "plant_restart_units_open",
    "unit_has_open_outage",
    "revision_changes_unit",
    "restart_before_event_start",
    "ends_event_mismatch",
    "revision_after_restart_date",
    "restart_in_future",
]

REVIEW_SQLSTATES: Final[dict[str, ReviewReason]] = {
    "RW003": "supersedes_not_current",
    "RW004": "family_mismatch",
    "RW005": "no_restart_match",
    "RW006": "unit_has_open_outage",
    "RW007": "revision_changes_unit",
    "RW008": "restart_before_event_start",
    "RW009": "ends_event_mismatch",
    "RW010": "revision_after_restart_date",
    "RW013": "plant_restart_units_open",
    "RW015": "restart_in_future",
}
SUPERSEDES_KEY: Final = "outage_event_supersedes_key"


@dataclass(frozen=True, slots=True)
class NewEventRow:
    event_type: EventType
    refinery_id: str
    refinery_unit_id: int | None
    whole_plant: bool
    start_date: date
    confidence: Confidence
    capacity_basis: CapacityBasis
    match_score: int
    capacity_affected_kbd: Decimal | None = None
    unit_label: str | None = None
    start_unknown: bool = False
    old_start: bool = False
    expected_restart_date: date | None = None
    cause: str | None = None
    supersedes: int | None = None
    known_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceLink:
    raw_document_id: int
    source_kind: SourceKind
    evidence: str
    extraction_id: int | None = None


@dataclass(frozen=True, slots=True)
class Written:
    id: int
    chain_id: int
    ends_event_id: int | None


@dataclass(frozen=True, slots=True)
class Review:
    reason: ReviewReason
    detail: str


@dataclass(frozen=True, slots=True)
class OutageRow:
    id: int
    chain_id: int
    supersedes: int | None
    ends_event_id: int | None
    event_type: EventType
    refinery_id: str
    refinery_unit_id: int | None
    whole_plant: bool
    unit_label: str | None
    capacity_affected_kbd: Decimal | None
    capacity_basis: CapacityBasis
    start_date: date
    start_unknown: bool
    old_start: bool
    expected_restart_date: date | None
    cause: str | None
    confidence: Confidence
    match_score: int
    known_at: datetime


def require_row(row: DictRow | None) -> DictRow:
    if row is None:
        raise RuntimeError("query returned no row")
    return row


def require_aware(value: datetime | None, name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")


def review_from(exc: psycopg.Error) -> Review | None:
    """Maps a trigger or constraint error to a review outcome. None means re-raise."""
    detail = exc.diag.message_primary or str(exc)
    if exc.sqlstate in REVIEW_SQLSTATES:
        return Review(REVIEW_SQLSTATES[exc.sqlstate], detail)
    if (
        isinstance(exc, psycopg.errors.UniqueViolation)
        and exc.diag.constraint_name == SUPERSEDES_KEY
    ):
        return Review("supersedes_not_current", detail)
    return None


def insert_source(conn: Conn, outage_event_id: int, source: SourceLink, role: str) -> int:
    """Inserts an event_source row. The database sets known_at."""
    cursor = conn.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, extraction_id, role,"
        " source_kind, evidence) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            outage_event_id,
            source.raw_document_id,
            source.extraction_id,
            role,
            source.source_kind,
            source.evidence,
        ),
    )
    found = require_row(cursor.fetchone())
    return int(found["id"])


def insert_event(conn: Conn, row: NewEventRow) -> Written:
    cursor = conn.execute(
        "INSERT INTO outage_event (event_type, refinery_id, refinery_unit_id, whole_plant,"
        " unit_label, capacity_affected_kbd, capacity_basis, start_date, start_unknown,"
        " old_start, expected_restart_date, cause, confidence, match_score, supersedes, known_at)"
        " VALUES (%(event_type)s, %(refinery_id)s, %(refinery_unit_id)s, %(whole_plant)s,"
        " %(unit_label)s, %(capacity_affected_kbd)s, %(capacity_basis)s, %(start_date)s,"
        " %(start_unknown)s, %(old_start)s, %(expected_restart_date)s, %(cause)s,"
        " %(confidence)s, %(match_score)s, %(supersedes)s, %(known_at)s)"
        " RETURNING id, chain_id, ends_event_id",
        asdict(row),
    )
    found = require_row(cursor.fetchone())
    return Written(int(found["id"]), int(found["chain_id"]), found["ends_event_id"])


def write_event(conn: Conn, row: NewEventRow, source: SourceLink) -> Written | Review:
    """Writes one row and its primary source in a savepoint. Rule failures return a Review."""
    require_aware(row.known_at, "known_at")
    try:
        with conn.transaction():
            written = insert_event(conn, row)
            insert_source(conn, written.id, source, "primary")
    except psycopg.Error as exc:
        review = review_from(exc)
        if review is None:
            raise
        return review
    return written


def add_corroboration(conn: Conn, outage_event_id: int, source: SourceLink) -> int:
    return insert_source(conn, outage_event_id, source, "corroboration")


def outage_as_of(conn: Conn, as_of: datetime) -> list[OutageRow]:
    require_aware(as_of, "as_of")
    rows = conn.execute("SELECT * FROM outage_as_of(%s) ORDER BY id", (as_of,)).fetchall()
    return [OutageRow(**r) for r in rows]


def match_restart(
    conn: Conn,
    refinery_id: str,
    refinery_unit_id: int | None,
    start_date: date,
    as_of: datetime,
    whole_plant: bool = False,
) -> int | None:
    require_aware(as_of, "as_of")
    cursor = conn.execute(
        "SELECT match_restart(%s, %s, %s, %s, %s) AS id",
        (refinery_id, refinery_unit_id, start_date, as_of, whole_plant),
    )
    found = require_row(cursor.fetchone())
    return None if found["id"] is None else int(found["id"])


def outage_duplicate_of(
    conn: Conn,
    refinery_id: str,
    refinery_unit_id: int | None,
    whole_plant: bool,
    start_date: date,
    start_unknown: bool,
    as_of: datetime,
) -> list[int]:
    require_aware(as_of, "as_of")
    rows = conn.execute(
        "SELECT c FROM outage_duplicate_of(%s, %s, %s, %s, %s, %s) AS c",
        (refinery_id, refinery_unit_id, whole_plant, start_date, start_unknown, as_of),
    ).fetchall()
    return [int(r["c"]) for r in rows]


def restart_duplicate_of(
    conn: Conn,
    refinery_id: str,
    refinery_unit_id: int | None,
    start_date: date,
    as_of: datetime,
    whole_plant: bool = False,
) -> int | None:
    require_aware(as_of, "as_of")
    cursor = conn.execute(
        "SELECT restart_duplicate_of(%s, %s, %s, %s, %s) AS id",
        (refinery_id, refinery_unit_id, start_date, as_of, whole_plant),
    )
    found = require_row(cursor.fetchone())
    return None if found["id"] is None else int(found["id"])
