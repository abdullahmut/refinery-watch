"""Row builders for the flare tables. Each insert goes through the table's insert trigger."""

import math
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.db.helpers import add_document
from tests.netguard import block_network

from refinery_watch.db.connect import Conn
from refinery_watch.flare.footprints import Location, write_footprint
from refinery_watch.flare.geometry import radius_bbox
from refinery_watch.flare.overpass import (
    FootprintChoice,
    choose_footprint,
    parse_elements,
)

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


FIRMS_KEY = "k" * 32
HANDMADE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "handmade"


def handmade(name: str) -> bytes:
    return (HANDMADE / name).read_bytes()


def firms_handler(
    noaa20: bytes | None = None,
    snpp: bytes | None = None,
    empty: bytes | None = None,
    fail: dict[str, int] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serves the handmade CSVs by satellite. `fail` maps a path fragment to a status."""
    noaa20 = noaa20 if noaa20 is not None else handmade("firms_area_viirs_noaa20_nrt_handmade.csv")
    snpp = snpp if snpp is not None else handmade("firms_area_viirs_snpp_nrt_handmade.csv")
    empty = empty if empty is not None else handmade("firms_area_empty_handmade.csv")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for fragment, status in (fail or {}).items():
            if fragment in path:
                return httpx.Response(status, content=b"nope")
        if "-95." not in path:
            return httpx.Response(200, content=empty)
        body = noaa20 if "VIIRS_NOAA20" in path else snpp
        return httpx.Response(200, content=body)

    return handler


def seed_plant(conn: Conn, refinery_id: str = "R9001") -> int:
    """A committed footprint: the handmade square for R9001, a circle for the others."""
    if refinery_id == "R9001":
        shapes = parse_elements(handmade("overpass_one_way_handmade.json"))
        place = Location("R9001", 29.745, -95.012, 2500, None, None, True)
        choice = choose_footprint(place.lat, place.lon, place.radius_m, None, shapes)
    else:
        lat, lon = {"R9002": (51.9, 4.3), "R9003": (1.27, 103.7)}[refinery_id]
        place = Location(refinery_id, lat, lon, 2500, None, None, False)
        choice = FootprintChoice(
            "point_radius", (), (), radius_bbox(place.lat, place.lon, place.radius_m)
        )
    found = write_footprint(conn, place, choice, None)
    assert found is not None
    conn.commit()
    return found


def start_job(conn: Conn, job: str = "fetch_nasa_firms") -> int:
    row = conn.execute("INSERT INTO job_run (job) VALUES (%s) RETURNING id", (job,)).fetchone()
    assert row is not None
    conn.commit()
    return int(row["id"])


def table_count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """For flare database tests: a real HTTP request or name lookup fails the test."""
    block_network(monkeypatch)


def add_hot_day(conn: Conn, refinery_id: str = "R9001", day: date | None = None) -> int:
    """A live flare_day that clears the alert threshold. Needs the plant's footprint."""
    fp = conn.execute(
        "SELECT id FROM plant_footprint WHERE refinery_id = %s ORDER BY id DESC", (refinery_id,)
    ).fetchone()
    footprint = int(fp["id"]) if fp else add_footprint(conn, refinery_id)
    return add_day(
        conn,
        footprint,
        day or datetime.now(UTC).date(),
        refinery_id=refinery_id,
        n_detections=3,
        frp_sum_mw=99,
        x=math.log(100),
        baseline_median=math.log(4),
        baseline_mad=0.3,
        z=7.5,
    )
