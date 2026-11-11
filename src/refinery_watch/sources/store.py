"""The one write path from a fetched document to the object store and raw_document."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from refinery_watch.db.connect import Conn
from refinery_watch.objects import RawStore
from refinery_watch.sources.published import Published

TermMatcher = Callable[[str], Sequence[str]]

DUPLICATE_CONSTRAINTS: Final = ("content_sha256", "object_key")


@dataclass(frozen=True, slots=True)
class Stored:
    raw_document_id: int
    object_key: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class Skipped:
    reason: Literal["known_hash", "known_url"]
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class NewDocument:
    source_id: str
    url: str
    original: bytes
    ext: Literal["html", "json"]
    content_type: str
    content_sha256: str
    headline: str | None
    body_chars: int
    fetched_at: datetime
    published: Published | None
    prefilter_terms: tuple[str, ...]


def _returned(row: DictRow | None) -> DictRow:
    if row is None:
        raise RuntimeError("insert returned no row")
    return row


def _require_aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def object_key(source_id: str, fetched_at: datetime, sha: str, ext: str) -> str:
    _require_aware(fetched_at, "fetched_at")
    day = fetched_at.astimezone(UTC)
    return f"raw/{source_id}/{day:%Y/%m/%d}/{sha}.{ext}"


def url_known(conn: Conn, urls: Sequence[str]) -> bool:
    row = conn.execute(
        "SELECT 1 AS found FROM raw_document WHERE url = ANY(%s) LIMIT 1", (list(urls),)
    ).fetchone()
    return row is not None


def _hash_known(conn: Conn, sha: str) -> bool:
    row = conn.execute(
        "SELECT 1 AS found FROM raw_document WHERE content_sha256 = %s", (sha,)
    ).fetchone()
    return row is not None


def store_document(
    conn: Conn,
    store: RawStore,
    job_run_id: int,
    doc: NewDocument,
    *,
    in_same_tx: Callable[[int], None] | None = None,
) -> Stored | Skipped:
    _require_aware(doc.fetched_at, "fetched_at")
    if doc.published is not None:
        _require_aware(doc.published.at, "published.at")
    if _hash_known(conn, doc.content_sha256):
        return Skipped("known_hash", doc.content_sha256)
    key = object_key(doc.source_id, doc.fetched_at, doc.content_sha256, doc.ext)
    store.put(key, doc.original, doc.content_type)
    terms = list(doc.prefilter_terms)
    published = doc.published
    duplicate = False
    new_id = 0
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO raw_document (source_id, job_run_id, url, content_sha256,"
                " object_key, content_type, headline, body_chars, fetched_at, published_at,"
                " published_tz, prefilter_passed, prefilter_terms)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    doc.source_id,
                    job_run_id,
                    doc.url,
                    doc.content_sha256,
                    key,
                    doc.content_type,
                    doc.headline,
                    doc.body_chars,
                    doc.fetched_at,
                    published.at if published else None,
                    published.tz if published else None,
                    len(terms) > 0,
                    terms,
                ),
            ).fetchone()
            new_id = int(_returned(row)["id"])
            if in_same_tx is not None:
                in_same_tx(new_id)
    except psycopg.errors.UniqueViolation as exc:
        name = exc.diag.constraint_name or ""
        if not any(part in name for part in DUPLICATE_CONSTRAINTS):
            raise
        duplicate = True
    if duplicate:
        conn.commit()
        return Skipped("known_hash", doc.content_sha256)
    conn.commit()
    return Stored(new_id, key, doc.content_sha256)


def flag_thin_page(conn: Conn, raw_document_id: int, url: str, body_chars: int, images: int) -> int:
    row = conn.execute(
        "INSERT INTO review_item (raw_document_id, candidate, reason)"
        " VALUES (%s, %s, 'thin_page') RETURNING id",
        (raw_document_id, Jsonb({"url": url, "body_chars": body_chars, "images": images})),
    ).fetchone()
    new_id = int(_returned(row)["id"])
    conn.commit()
    return new_id


RunStatus = Literal["ok", "failed", "skipped"]
INTERVAL_FACTOR: Final = 0.9
RUN_BUDGET: Final = timedelta(minutes=20)
NOTE_MAX_CHARS: Final = 1000
CLEAR_STATUSES: Final = frozenset({"open", "api_key_required"})
CLEAR_PREFIX: Final = "allowed"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    rows_in: int
    rows_out: int
    note: str | None


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: str
    feed_url: str
    fetch_interval_min: int
    robots_status: str


class RunNotRunning(RuntimeError):  # noqa: N818
    """finish_run found no running row, for example because a later start swept it as stale."""


class SourceMissing(RuntimeError):  # noqa: N818
    """The source row is not in the database."""


class SourceNotCleared(RuntimeError):  # noqa: N818
    """The source robots status is not a clear allow."""


def job_name(source_id: str) -> str:
    return "fetch_" + source_id


def robots_cleared(status: str) -> bool:
    """True only for a stated allow. Unchecked, refused and unknown statuses are not."""
    return status in CLEAR_STATUSES or status.startswith(CLEAR_PREFIX)


def source_row(conn: Conn, source_id: str) -> SourceRow:
    row = conn.execute(
        "SELECT id, feed_url, fetch_interval_min, robots_status FROM source WHERE id = %s",
        (source_id,),
    ).fetchone()
    if row is None:
        raise SourceMissing(f"source {source_id} not loaded; run the source seed loader")
    found = SourceRow(
        row["id"], row["feed_url"], int(row["fetch_interval_min"]), row["robots_status"]
    )
    if not robots_cleared(found.robots_status):
        raise SourceNotCleared(f"source {source_id} robots status is not a clear allow")
    return found


def _log_skipped(conn: Conn, job: str, why: str) -> None:
    conn.execute(
        "INSERT INTO job_run (job, status, finished_at, error)"
        " VALUES (%s, 'skipped', clock_timestamp(), %s)",
        (job, why),
    )
    conn.commit()


def start_run(
    conn: Conn, job: str, interval_min: int, stale_after: timedelta = timedelta(minutes=40)
) -> int | None:
    conn.execute(
        "UPDATE job_run SET status = 'failed', finished_at = clock_timestamp(),"
        " error = 'stale: no finish recorded'"
        " WHERE job = %s AND status = 'running'"
        " AND started_at < clock_timestamp() - make_interval(secs => %s)",
        (job, stale_after.total_seconds()),
    )
    recent = conn.execute(
        "SELECT 1 AS found FROM job_run WHERE job = %s AND status IN ('ok', 'failed')"
        " AND started_at > clock_timestamp() - make_interval(secs => %s) LIMIT 1",
        (job, INTERVAL_FACTOR * interval_min * 60),
    ).fetchone()
    if recent is not None:
        _log_skipped(conn, job, "interval")
        return None
    busy = False
    new_id = 0
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO job_run (job) VALUES (%s) RETURNING id", (job,)
            ).fetchone()
            new_id = int(_returned(row)["id"])
    except psycopg.errors.UniqueViolation:
        busy = True
    if busy:
        _log_skipped(conn, job, "already running")
        return None
    conn.commit()
    return new_id


def finish_run(conn: Conn, run_id: int, outcome: RunOutcome) -> None:
    note = outcome.note[:NOTE_MAX_CHARS] if outcome.note else None
    conn.rollback()
    cursor = conn.execute(
        "UPDATE job_run SET status = %s, finished_at = clock_timestamp(), rows_in = %s,"
        " rows_out = %s, error = %s WHERE id = %s AND status = 'running'",
        (outcome.status, outcome.rows_in, outcome.rows_out, note, run_id),
    )
    updated = cursor.rowcount
    conn.commit()
    if updated != 1:
        raise RunNotRunning(f"job_run {run_id} was not running, so its result was not recorded")


@dataclass(frozen=True, slots=True)
class PriceRow:
    series: str
    observed_on: date
    value: Decimal
    unit: str


PRICE_SCALE: Final = Decimal("0.0001")


def write_prices(
    conn: Conn,
    rows: Sequence[PriceRow],
    source_id: str,
    raw_document_id: int | None,
    *,
    commit: bool = True,
) -> int:
    """Inserts rows that are new or differ from the latest known value. Never updates."""
    inserted = 0
    for row in rows:
        value = row.value.quantize(PRICE_SCALE)
        latest = conn.execute(
            "SELECT value FROM price WHERE series = %s AND observed_on = %s"
            " ORDER BY known_at DESC LIMIT 1",
            (row.series, row.observed_on),
        ).fetchone()
        if latest is not None and Decimal(latest["value"]) == value:
            continue
        conn.execute(
            "INSERT INTO price (series, observed_on, value, unit, source_id, raw_document_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (row.series, row.observed_on, value, row.unit, source_id, raw_document_id),
        )
        inserted += 1
    if commit:
        conn.commit()
    return inserted
