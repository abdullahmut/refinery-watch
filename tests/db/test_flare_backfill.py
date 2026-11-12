import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx
import psycopg
import pytest
from tests.db.flare_helpers import (
    FIRMS_KEY,
    handmade,
    seed_plant,
    start_job,
    table_count,
)
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, make_fetcher

from refinery_watch.db.connect import Conn
from refinery_watch.flare.fetch import backfill_plans, run_backfill
from refinery_watch.flare.firms import QueryBox, parse_availability, query_boxes
from refinery_watch.flare.footprints import Location, current_footprints, write_footprint
from refinery_watch.flare.geometry import BBox, radius_bbox
from refinery_watch.flare.overpass import FootprintChoice
from refinery_watch.sources.http import Fetcher
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

HEADER = handmade("firms_area_empty_handmade.csv").decode()
AVAILABILITY = handmade("firms_data_availability_handmade.csv")
SP_LAST = date(2026, 8, 31)
BOX = QueryBox(BBox(-95.2, 29.6, -94.8, 29.9), ("R9001",))


@pytest.fixture(autouse=True)
def quiet_http_logs() -> Iterator[None]:
    names = ("httpx", "httpcore")
    old = [logging.getLogger(n).level for n in names]
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)
    yield
    for name, level in zip(names, old, strict=True):
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    seed_plant(conn)
    yield conn
    conn.rollback()
    conn.close()


def one_detection_on(start: date, hour: int = 6) -> bytes:
    row = f"29.74600,-95.01100,330.5,0.39,0.36,{start},{hour:02d}15,N20,VIIRS,n,2.0,295.1,5.00,N\n"
    return (HEADER + row).encode()


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if "/data_availability/" in path:
        return httpx.Response(200, content=AVAILABILITY)
    start = date.fromisoformat(path.rsplit("/", 1)[1])
    return httpx.Response(200, content=one_detection_on(start))


def go(
    conn: Conn,
    fetcher: Fetcher,
    first: date,
    last: date,
    max_requests: int = 150,
) -> RunOutcome:
    run_id = start_job(conn, "flare_backfill")
    outcome = run_backfill(
        conn, fetcher, FakeStore(), run_id, FIRMS_KEY, first, last, max_requests, float("inf")
    )
    finish_run(conn, run_id, outcome)
    return outcome


def plan_view(conn: Conn, first: date, last: date) -> list[tuple[str, str, date, int]]:
    availability = parse_availability(AVAILABILITY)
    plans = backfill_plans(conn, [BOX], availability, first, last)
    return [(p.satellite, p.product, p.start, p.range_days) for p in plans]


def test_sp_up_to_the_sp_last_date_then_nrt(app: Conn) -> None:
    got = plan_view(app, date(2026, 8, 28), date(2026, 9, 4))
    assert got == [
        ("SNPP", "SP", date(2026, 8, 28), 4),
        ("NOAA20", "SP", date(2026, 8, 28), 4),
        ("SNPP", "NRT", date(2026, 9, 1), 4),
        ("NOAA20", "NRT", date(2026, 9, 1), 4),
    ]


def test_chunks_are_five_days_and_the_last_may_be_shorter(app: Conn) -> None:
    got = plan_view(app, date(2026, 6, 1), date(2026, 6, 12))
    assert [(d, n) for sat, _, d, n in got if sat == "SNPP"] == [
        (date(2026, 6, 1), 5),
        (date(2026, 6, 6), 5),
        (date(2026, 6, 11), 2),
    ]


def test_a_missing_sp_entry_falls_back_to_nrt(app: Conn) -> None:
    availability = {"VIIRS_SNPP_NRT": (date(2012, 1, 1), date(2026, 11, 12))}
    plans = backfill_plans(app, [BOX], availability, date(2026, 6, 1), date(2026, 6, 5))
    assert {(p.satellite, p.product) for p in plans} == {("SNPP", "NRT"), ("NOAA20", "NRT")}


def test_an_empty_range_gives_no_plans(app: Conn) -> None:
    assert plan_view(app, date(2026, 6, 5), date(2026, 6, 4)) == []


def test_a_run_writes_backfilled_days(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(handler)
    outcome = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 5))
    assert (outcome.status, outcome.rows_in, outcome.rows_out) == ("ok", 3, 2)
    assert "data_availability" in net.requests[0].url.path
    rows = app.execute(
        "SELECT day, backfilled, available_at, known_at, n_detections FROM flare_day ORDER BY day"
    ).fetchall()
    assert [r["day"] for r in rows] == [date(2026, 6, d) for d in range(1, 6)]
    assert all(r["backfilled"] for r in rows)
    first = rows[0]
    assert first["available_at"] == datetime(2026, 6, 1, 9, 15, tzinfo=UTC)
    assert first["available_at"] <= first["known_at"]
    assert rows[1]["available_at"] == datetime(2026, 6, 3, 3, tzinfo=UTC)
    assert (first["n_detections"], rows[1]["n_detections"]) == (2, 0)
    assert {r["product"] for r in app.execute("SELECT product FROM flare_coverage")} == {"SP"}


def test_the_availability_request_is_redacted_nowhere_stored(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(handler)
    go(app, fetcher, date(2026, 6, 1), date(2026, 6, 5))
    urls = [r["url"] for r in app.execute("SELECT url FROM raw_document").fetchall()]
    assert urls
    assert all(FIRMS_KEY not in u for u in urls)


def test_a_run_stops_at_max_requests_and_the_next_one_picks_up(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(handler)
    first = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 10), max_requests=3)
    assert (first.status, first.rows_in) == ("ok", 3)
    assert first.note == "2 requests left; run again"
    area = [r for r in net.requests if "/area/" in r.url.path]
    assert len(area) == 2
    assert {r.url.path.rsplit("/", 1)[1] for r in area} == {"2026-06-01"}
    second = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 10), max_requests=3)
    assert second.rows_in == 3
    area_two = [r for r in net.requests if "/area/" in r.url.path][2:]
    assert {r.url.path.rsplit("/", 1)[1] for r in area_two} == {"2026-06-06"}
    third = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 10), max_requests=3)
    assert (third.rows_in, third.rows_out, third.note) == (1, 0, None)


def test_covered_chunks_are_skipped(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(handler)
    go(app, fetcher, date(2026, 6, 1), date(2026, 6, 5))
    assert plan_view(app, date(2026, 6, 1), date(2026, 6, 5)) == []
    assert len(plan_view(app, date(2026, 6, 1), date(2026, 6, 6))) == 2


def test_a_partly_covered_chunk_is_fetched_again(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(handler)
    go(app, fetcher, date(2026, 6, 1), date(2026, 6, 3))
    got = plan_view(app, date(2026, 6, 1), date(2026, 6, 5))
    assert [(p, d, n) for _, p, d, n in got] == [("SP", date(2026, 6, 1), 5)] * 2


def test_a_failed_satellite_leaves_its_chunk_unscored_and_unskipped(app: Conn) -> None:
    def flaky(request: httpx.Request) -> httpx.Response:
        if "VIIRS_NOAA20" in request.url.path:
            return httpx.Response(404)
        return handler(request)

    fetcher, _, _ = make_fetcher(flaky)
    outcome = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 5))
    assert outcome.status == "failed"
    assert table_count(app, "flare_day") == 0
    assert plan_view(app, date(2026, 6, 1), date(2026, 6, 5)) == [
        ("NOAA20", "SP", date(2026, 6, 1), 5)
    ]


def test_a_bad_availability_reply_fails_the_run_before_any_area_request(app: Conn) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"Invalid MAP_KEY.")

    fetcher, net, _ = make_fetcher(bad)
    outcome = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 5))
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert FIRMS_KEY not in outcome.note
    assert len(net.requests) == 1


def test_without_footprints_the_backfill_fails_clearly(seeded_schema: str, app_url: str) -> None:
    conn = open_in_schema(app_url, seeded_schema)
    try:
        fetcher, net, _ = make_fetcher(handler)
        outcome = go(conn, fetcher, date(2026, 6, 1), date(2026, 6, 5))
        assert outcome.status == "failed"
        assert net.requests == []
    finally:
        conn.rollback()
        conn.close()


def test_an_inverted_range_is_refused(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(handler)
    outcome = go(app, fetcher, date(2026, 6, 5), date(2026, 6, 1))
    assert outcome.status == "failed"
    assert outcome.note == "the first day is after the last day"
    assert net.requests == []


def test_later_days_are_scored_against_earlier_backfilled_ones(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(handler)
    go(app, fetcher, date(2026, 6, 1), date(2026, 6, 5))
    row = app.execute(
        "SELECT baseline_days FROM flare_day WHERE day = %s", (date(2026, 6, 5),)
    ).fetchone()
    assert row == {"baseline_days": 4}


def test_the_availability_call_counts_against_max_requests(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(handler)
    outcome = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 10), max_requests=1)
    assert [r.url.path.split("/")[2] for r in net.requests] == ["data_availability"]
    assert (outcome.status, outcome.rows_in) == ("ok", 1)
    assert outcome.note == "4 requests left; run again"
    two = go(app, fetcher, date(2026, 6, 1), date(2026, 6, 10), max_requests=2)
    assert two.rows_in == 2
    assert len([r for r in net.requests if "/area/" in r.url.path]) == 1


def _add_refinery(owner_url: str, schema: str, refinery_id: str) -> None:
    with psycopg.connect(owner_url, options=f"-c search_path={schema}") as conn:
        conn.execute(
            "INSERT INTO refinery (id, name, country, region, status)"
            " VALUES (%s, %s, 'United States', 'US Gulf', 'operating')",
            (refinery_id, f"Plant {refinery_id}"),
        )


def _circle(conn: Conn, refinery_id: str, lat: float, lon: float) -> None:
    place = Location(refinery_id, lat, lon, 2500, None, None, False)
    choice = FootprintChoice("point_radius", (), (), radius_bbox(lat, lon, 2500))
    assert write_footprint(conn, place, choice, None) is not None
    conn.commit()


def test_a_plant_added_inside_an_unchanged_box_is_still_backfilled(
    seeded_schema: str, app_url: str, pg_server: str
) -> None:
    for refinery_id in ("R9005", "R9006"):
        _add_refinery(pg_server, seeded_schema, refinery_id)
    conn = open_in_schema(app_url, seeded_schema)
    try:
        seed_plant(conn)
        _circle(conn, "R9006", 29.40, -95.40)
        before = [b.bbox for b in query_boxes(current_footprints(conn))]
        conn.rollback()

        def area(request: httpx.Request) -> httpx.Response:
            if "/data_availability/" in request.url.path:
                return httpx.Response(200, content=AVAILABILITY)
            return httpx.Response(200, content=detection_at(date(2026, 6, 1), 29.60, -95.20))

        fetcher, _, _ = make_fetcher(area)
        assert go(conn, fetcher, date(2026, 6, 1), date(2026, 6, 5)).status == "ok"
        _circle(conn, "R9005", 29.60, -95.20)
        assert [b.bbox for b in query_boxes(current_footprints(conn))] == before
        conn.rollback()
        second = go(conn, fetcher, date(2026, 6, 1), date(2026, 6, 5))
        assert second.status == "ok"
        cover = conn.execute(
            "SELECT count(*) AS n FROM flare_coverage WHERE refinery_id = 'R9005'"
        ).fetchone()
        assert cover == {"n": 2 * 5}
        found = conn.execute(
            "SELECT count(*) AS n FROM flare_detection WHERE refinery_id = 'R9005'"
        ).fetchone()
        assert found == {"n": 2}
        third = go(conn, fetcher, date(2026, 6, 1), date(2026, 6, 5))
        assert third.rows_in == 1
    finally:
        conn.rollback()
        conn.close()


def detection_at(start: date, lat: float, lon: float) -> bytes:
    row = f"{lat:.5f},{lon:.5f},330.5,0.39,0.36,{start},0615,N20,VIIRS,n,2.0,295.1,5.00,N\n"
    return (HEADER + row).encode()
