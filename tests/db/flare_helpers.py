"""Row builders for the flare tables. Each insert goes through the table's insert trigger."""

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from tests.db.helpers import add_document

from refinery_watch.db.connect import Conn

SHA = "ab" * 32


def _insert(conn: Conn, table: str, row: dict[str, Any]) -> int:
    cols = ", ".join(row)
    marks = ", ".join(f"%({k})s" for k in row)
    found = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING id", row)
    result = found.fetchone()
    assert result is not None
    return int(result["id"])


def add_footprint(conn: Conn, refinery_id: str = "R9001", **over: Any) -> int:
    row: dict[str, Any] = {
        "refinery_id": refinery_id,
        "kind": "point_radius",
        "center_lat": 29.745,
        "center_lon": -95.012,
        "radius_m": 2500,
        "vertex_count": 0,
        "west": -95.05,
        "south": 29.71,
        "east": -94.97,
        "north": 29.78,
        "geometry_sha256": SHA,
        "location_verified": False,
    }
    row.update(over)
    return _insert(conn, "plant_footprint", row)


def add_day(
    conn: Conn, footprint_id: int, day: date, refinery_id: str = "R9001", **over: Any
) -> int:
    """A live, scored flare_day. Pass backfilled=True with available_at for a backfilled row."""
    row: dict[str, Any] = {
        "refinery_id": refinery_id,
        "footprint_id": footprint_id,
        "day": day,
        "n_detections": 0,
        "frp_sum_mw": 0,
        "products": ["SNPP:NRT", "NOAA20:NRT"],
        "x": 0.0,
        "baseline_days": 90,
        "z": 0.0,
        "backfilled": False,
    }
    row.update(over)
    return _insert(conn, "flare_day", row)


def add_alert(conn: Conn, flare_day_id: int, flare_date: date, **over: Any) -> int:
    row: dict[str, Any] = {
        "refinery_id": "R9001",
        "flare_day_id": flare_day_id,
        "flare_date": flare_date,
        "figures": "{}",
        "evidence_found": False,
        "subject": "Flaring alert",
        "body_text": "t",
        "body_html": "<p>t</p>",
        "send_fingerprint": SHA,
        "idempotency_key": f"{flare_day_id:032x}",
    }
    row.update(over)
    return _insert(conn, "flare_alert", row)


def add_attempt(conn: Conn, alert_id: int, attempt: int = 1, **over: Any) -> int:
    row: dict[str, Any] = {
        "flare_alert_id": alert_id,
        "attempt": attempt,
        "status": "send_failed",
        "http_status": 500,
        "error": "500 internal_server_error",
    }
    row.update(over)
    return _insert(conn, "flare_alert_attempt", row)


def add_detection_and_coverage(
    conn: Conn, footprint_id: int, day: date, known_at: str | None = None
) -> None:
    doc = add_document(conn, 900 + day.toordinal() % 10000)
    conn.execute(
        "INSERT INTO flare_detection (refinery_id, footprint_id, satellite, product, acquired_at,"
        " latitude, longitude, frp_mw, confidence, daynight, raw_document_id, known_at)"
        " VALUES ('R9001', %s, 'SNPP', 'NRT', %s, 29.7, -95.0, 4.5, 'n', 'N', %s,"
        " coalesce(%s::timestamptz, now()))",
        (footprint_id, f"{day} 07:42+00", doc, known_at),
    )
    conn.execute(
        "INSERT INTO flare_coverage (refinery_id, day, satellite, product, raw_document_id,"
        " known_at) VALUES ('R9001', %s, 'SNPP', 'NRT', %s, coalesce(%s::timestamptz, now()))",
        (day, doc, known_at),
    )


def add_coverage(
    conn: Conn,
    day: date,
    document_id: int,
    satellites: tuple[str, ...] = ("SNPP", "NOAA20"),
    product: str = "NRT",
    refinery_id: str = "R9001",
) -> None:
    for satellite in satellites:
        conn.execute(
            "INSERT INTO flare_coverage (refinery_id, day, satellite, product, raw_document_id)"
            " VALUES (%s, %s, %s, %s, %s)",
            (refinery_id, day, satellite, product, document_id),
        )


def add_detection(
    conn: Conn,
    footprint_id: int,
    day: date,
    frp: float,
    document_id: int,
    satellite: str = "SNPP",
    product: str = "NRT",
    hour: int = 7,
    minute: int = 0,
    refinery_id: str = "R9001",
) -> None:
    conn.execute(
        "INSERT INTO flare_detection (refinery_id, footprint_id, satellite, product, acquired_at,"
        " latitude, longitude, frp_mw, confidence, daynight, raw_document_id)"
        " VALUES (%s, %s, %s, %s, (%s::date + make_interval(hours => %s, mins => %s))"
        " AT TIME ZONE 'UTC', 29.7, -95.0, %s, 'n', 'N', %s)",
        (refinery_id, footprint_id, satellite, product, day, hour, minute, frp, document_id),
    )


def seed_days(
    conn: Conn,
    footprint_id: int,
    first: date,
    values: Sequence[float | None],
    refinery_id: str = "R9001",
) -> list[date]:
    """One covered day per value: None leaves the day out, 0 has no detection, else one."""
    document_id = add_document(conn, 5000 + first.toordinal() % 1000)
    days: list[date] = []
    for offset, value in enumerate(values):
        day = first + timedelta(days=offset)
        if value is None:
            continue
        add_coverage(conn, day, document_id, refinery_id=refinery_id)
        if value > 0:
            add_detection(conn, footprint_id, day, value, document_id, refinery_id=refinery_id)
        days.append(day)
    return days


def write_day(
    conn: Conn, day: date, backfilled: bool = False, refinery_id: str = "R9001"
) -> int | None:
    row = conn.execute(
        "SELECT flare_day_write(%s, %s, %s, NULL) AS id", (refinery_id, day, backfilled)
    ).fetchone()
    assert row is not None
    return None if row["id"] is None else int(row["id"])


def current_day(conn: Conn, day: date, refinery_id: str = "R9001") -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM flare_day_current_as_of(clock_timestamp())"
        " WHERE refinery_id = %s AND day = %s",
        (refinery_id, day),
    ).fetchone()
    assert row is not None
    return dict(row)
