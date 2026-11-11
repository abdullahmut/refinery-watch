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
    "ambiguous_duplicate",
    "restart_date_unknown",
    "invalid_restart_fields",
    "start_unknown_high_confidence",
    "old_start_not_low",
    "whole_plant_has_unit",
    "invalid_capacity",
    "match_score_out_of_range",
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
LOCK_TIMEOUT = "10s"


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
class Duplicate:
    """The report is already in the table. Its source was added as corroboration."""

    chain_id: int
    corroborated_event_id: int


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


def invalid_capacity(row: NewEventRow) -> bool:
    amount = row.capacity_affected_kbd
    if (row.capacity_basis == "not_stated") != (amount is None):
        return True
    return amount is not None and not (amount.is_finite() and amount > 0)


def validate(row: NewEventRow) -> Review | None:
    """The row-level rules of docs/definitions.md, checked before any insert."""
    is_restart = row.event_type == "restart"
    checks: list[tuple[bool, ReviewReason, str]] = [
        (is_restart and row.start_unknown, "restart_date_unknown", "a restart needs a known date"),
        (
            is_restart and (row.old_start or row.expected_restart_date is not None),
            "invalid_restart_fields",
            "a restart cannot carry old_start or an expected restart date",
        ),
        (
            row.start_unknown and row.confidence == "high",
            "start_unknown_high_confidence",
            "an unknown start date cannot have high confidence",
        ),
        (
            row.old_start and row.confidence != "low",
            "old_start_not_low",
            "an old start date must have low confidence",
        ),
        (
            row.whole_plant and row.refinery_unit_id is not None,
            "whole_plant_has_unit",
            "a whole-plant row cannot name a unit",
        ),
        (
            invalid_capacity(row),
            "invalid_capacity",
            "capacity must be a finite positive number exactly when its basis is stated",
        ),
        (
            not 0 <= row.match_score <= 100,
            "match_score_out_of_range",
            "match_score must be between 0 and 100",
        ),
    ]
    for failed, reason, detail in checks:
        if failed:
            return Review(reason, detail)
    return None


def database_now(conn: Conn) -> datetime:
    cursor = conn.execute("SELECT clock_timestamp() AS now")
    now: datetime = require_row(cursor.fetchone())["now"]
    return now


def set_lock_timeout(conn: Conn, value: str) -> str:
    """Sets lock_timeout for the current transaction. Returns the previous value."""
    cursor = conn.execute("SELECT current_setting('lock_timeout') AS old")
    previous: str = require_row(cursor.fetchone())["old"]
    conn.execute("SELECT set_config('lock_timeout', %s, true)", (value,))
    return previous


def is_new_outage(row: NewEventRow) -> bool:
    return row.supersedes is None and row.event_type != "restart"


def corroborate(conn: Conn, chain_id: int, source: SourceLink) -> Duplicate:
    """Links the source to the newest row of the chain. A document already linked is skipped."""
    cursor = conn.execute("SELECT max(id) AS id FROM outage_event WHERE chain_id = %s", (chain_id,))
    event_id = int(require_row(cursor.fetchone())["id"])
    try:
        with conn.transaction():
            add_corroboration(conn, event_id, source)
    except psycopg.errors.UniqueViolation:
        pass
    return Duplicate(chain_id, event_id)


def write_event(conn: Conn, row: NewEventRow, source: SourceLink) -> Written | Review | Duplicate:
    """Writes one row and its primary source in a savepoint.

    A new outage that matches existing chains, open or closed, is not written. One match returns
    a Duplicate and the source is added as corroboration. Several matches return a Review.
    Rule failures return a Review. Other database errors are raised.
    """
    require_aware(row.known_at, "known_at")
    invalid = validate(row)
    if invalid is not None:
        return invalid
    if is_new_outage(row):
        as_of = row.known_at or database_now(conn)
        chains = outage_duplicate_of(
            conn,
            row.refinery_id,
            row.refinery_unit_id,
            row.whole_plant,
            row.start_date,
            row.start_unknown,
            as_of,
        )
        if len(chains) > 1:
            return Review("ambiguous_duplicate", f"matches chains {chains}")
        if chains:
            return corroborate(conn, chains[0], source)
    try:
        with conn.transaction():
            previous = set_lock_timeout(conn, LOCK_TIMEOUT)
            written = insert_event(conn, row)
            insert_source(conn, written.id, source, "primary")
            set_lock_timeout(conn, previous)
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
