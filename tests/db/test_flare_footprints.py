from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, Handler, make_fetcher

from refinery_watch.db.connect import Conn
from refinery_watch.flare.footprints import (
    Location,
    current_footprints,
    geometry_sha256,
    run_footprints,
    write_footprint,
)
from refinery_watch.flare.overpass import (
    FootprintChoice,
    choose_footprint,
    parse_elements,
    radius_bbox,
)
from refinery_watch.sources.http import Fetcher
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

HANDMADE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "handmade"
ONE_WAY = (HANDMADE / "overpass_one_way_handmade.json").read_bytes()
EMPTY = (HANDMADE / "overpass_empty_handmade.json").read_bytes()
REMARK = (HANDMADE / "overpass_remark_handmade.json").read_bytes()
R1 = Location("R9001", 29.745, -95.012, 2500, None, "https://example.test/a", True)
R2 = Location("R9002", 51.9, 4.3, 2500, None, None, False)
R3 = Location("R9003", 1.27, 103.7, 2500, None, None, False)


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def polygon_choice() -> FootprintChoice:
    return choose_footprint(R1.lat, R1.lon, R1.radius_m, None, parse_elements(ONE_WAY))


def circle_choice(location: Location = R1) -> FootprintChoice:
    return FootprintChoice(
        "point_radius", (), (), radius_bbox(location.lat, location.lon, location.radius_m)
    )


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def start(conn: Conn) -> int:
    found = conn.execute("INSERT INTO job_run (job) VALUES ('fetch_osm_overpass') RETURNING id")
    row = found.fetchone()
    assert row is not None
    conn.commit()
    return int(row["id"])


def go(
    conn: Conn,
    fetcher: Fetcher,
    store: FakeStore,
    locations: list[Location],
    max_requests: int = 100,
) -> RunOutcome:
    run_id = start(conn)
    outcome = run_footprints(conn, fetcher, store, run_id, locations, max_requests, float("inf"))
    finish_run(conn, run_id, outcome)
    return outcome


def by_plant(bodies: dict[str, bytes]) -> Handler:
    """Answers by the point in the query: a plant id keyed by its latitude text."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["data"]
        for key, body in bodies.items():
            if key in query:
                return httpx.Response(200, content=body)
        return httpx.Response(200, content=EMPTY)

    return handler


def test_writing_the_same_geometry_twice_inserts_once(app: Conn) -> None:
    first = write_footprint(app, R1, polygon_choice(), None)
    again = write_footprint(app, R1, polygon_choice(), None)
    assert first is not None
    assert again is None
    assert count(app, "plant_footprint") == 1


def test_a_new_geometry_supersedes_and_the_old_one_is_still_visible_as_of(app: Conn) -> None:
    first = write_footprint(app, R1, circle_choice(), None)
    assert first is not None
    app.commit()
    second = write_footprint(app, R1, polygon_choice(), None)
    assert second is not None
    row = app.execute(
        "SELECT supersedes, kind, approximate FROM plant_footprint WHERE id = %s", (second,)
    ).fetchone()
    assert row == {"supersedes": first, "kind": "osm_polygon", "approximate": False}
    stamp = app.execute("SELECT known_at FROM plant_footprint WHERE id = %s", (second,)).fetchone()
    assert stamp is not None
    before = app.execute(
        "SELECT id FROM footprint_current_as_of(%s - interval '1 microsecond')",
        (stamp["known_at"],),
    ).fetchall()
    assert [r["id"] for r in before] == [first]
    now = app.execute("SELECT id FROM footprint_current_as_of(clock_timestamp())").fetchall()
    assert [r["id"] for r in now] == [second]


def test_the_radius_circle_is_flagged_approximate(app: Conn) -> None:
    write_footprint(app, R1, circle_choice(), None)
    row = app.execute(
        "SELECT approximate, radius_m, rings, vertex_count, location_verified,"
        " location_source_url FROM plant_footprint"
    ).fetchone()
    assert row == {
        "approximate": True,
        "radius_m": 2500,
        "rings": None,
        "vertex_count": 0,
        "location_verified": True,
        "location_source_url": "https://example.test/a",
    }


def test_current_footprints_round_trips_rings_region_and_box(app: Conn) -> None:
    choice = polygon_choice()
    write_footprint(app, R1, choice, None)
    write_footprint(app, R2, circle_choice(R2), None)
    first, second = current_footprints(app)
    assert (first.refinery_id, first.kind, first.region) == ("R9001", "osm_polygon", "US Gulf")
    assert first.rings == choice.rings
    assert first.bbox.west <= choice.bbox.west
    assert first.bbox.east >= choice.bbox.east
    assert first.radius_m is None
    assert (second.refinery_id, second.kind, second.region) == (
        "R9002",
        "point_radius",
        "NW Europe",
    )
    assert second.radius_m == 2500
    assert second.contains(R2.lat, R2.lon)
    assert first.contains(29.746, -95.011)
    assert not first.contains(29.77, -95.012)


def test_geometry_hash_changes_with_the_point_the_radius_and_the_outline() -> None:
    base = geometry_sha256(circle_choice(), R1.lat, R1.lon, R1.radius_m)
    assert len(base) == 64
    assert base == geometry_sha256(circle_choice(), R1.lat, R1.lon, R1.radius_m)
    assert base != geometry_sha256(circle_choice(), R1.lat + 0.001, R1.lon, R1.radius_m)
    assert base != geometry_sha256(circle_choice(), R1.lat, R1.lon, 3000)
    assert base != geometry_sha256(polygon_choice(), R1.lat, R1.lon, R1.radius_m)
    polygon = polygon_choice()
    assert geometry_sha256(polygon, R1.lat, R1.lon, 2500) == geometry_sha256(
        polygon, R1.lat, R1.lon, 9000
    )


def test_run_stops_at_max_requests_and_stores_each_reply(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(by_plant({}))
    store = FakeStore()
    outcome = go(app, fetcher, store, [R1, R2, R3], max_requests=2)
    assert (outcome.status, outcome.rows_in, outcome.rows_out) == ("ok", 2, 2)
    assert outcome.note == "1 plants left for the next run"
    assert len(net.requests) == 2
    docs = app.execute("SELECT source_id, url, object_key FROM raw_document").fetchall()
    assert len(docs) == 2
    assert {d["source_id"] for d in docs} == {"osm_overpass"}
    assert all(d["object_key"].endswith(".json") for d in docs)
    assert all("around%3A5000" in d["url"] for d in docs)
    assert count(app, "plant_footprint") == 2


def test_the_next_run_takes_plants_without_a_footprint_first(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(by_plant({}))
    go(app, fetcher, FakeStore(), [R1, R2, R3], max_requests=2)
    first_two = {r.url.params["data"].split("around:5000,")[1].split(",")[0] for r in net.requests}
    assert first_two == {"29.74500", "51.90000"}
    fetcher2, net2, _ = make_fetcher(by_plant({}))
    outcome = go(app, fetcher2, FakeStore(), [R1, R2, R3], max_requests=1)
    assert outcome.note == "2 plants left for the next run"
    assert "1.27000" in net2.requests[0].url.params["data"]


def test_then_the_oldest_footprint_is_refreshed(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(by_plant({}))
    go(app, fetcher, FakeStore(), [R1, R2], max_requests=2)
    fetcher2, net2, _ = make_fetcher(by_plant({}))
    go(app, fetcher2, FakeStore(), [R2, R1], max_requests=1)
    assert "29.74500" in net2.requests[0].url.params["data"]


def test_a_second_run_with_the_same_replies_writes_nothing(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(by_plant({"29.74500": ONE_WAY}))
    store = FakeStore()
    go(app, fetcher, store, [R1])
    outcome = go(app, fetcher, store, [R1])
    assert (outcome.status, outcome.rows_in, outcome.rows_out) == ("ok", 1, 0)
    assert count(app, "raw_document") == 1
    assert count(app, "plant_footprint") == 1
    assert len(store.objects) == 1


def test_a_changed_reply_adds_a_superseding_footprint(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(by_plant({}))
    go(app, fetcher, FakeStore(), [R1])
    fetcher2, _, _ = make_fetcher(by_plant({"29.74500": ONE_WAY}))
    outcome = go(app, fetcher2, FakeStore(), [R1])
    assert outcome.rows_out == 1
    rows = app.execute("SELECT kind, supersedes FROM plant_footprint ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["point_radius", "osm_polygon"]
    assert rows[1]["supersedes"] is not None


def test_a_remark_reply_fails_that_plant_and_the_others_go_on(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(by_plant({"51.90000": REMARK}))
    outcome = go(app, fetcher, FakeStore(), [R1, R2, R3])
    assert outcome.status == "failed"
    assert (outcome.rows_in, outcome.rows_out) == (3, 2)
    assert outcome.note is not None
    assert "R9002" in outcome.note
    assert count(app, "plant_footprint") == 2


def test_an_unknown_refinery_is_reported_not_fetched(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(by_plant({}))
    ghost = replace(R1, refinery_id="R9777")
    outcome = go(app, fetcher, FakeStore(), [ghost, R1])
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert "R9777 is not a known refinery" in outcome.note
    assert len(net.requests) == 1


def test_a_fetch_error_is_noted_and_a_blocked_host_stops_the_run(app: Conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    fetcher, net, _ = make_fetcher(handler)
    outcome = go(app, fetcher, FakeStore(), [R1, R2])
    assert outcome.status == "failed"
    assert len(net.requests) == 1
    assert outcome.note is not None
    assert "stopped" in outcome.note
    assert count(app, "plant_footprint") == 0


def test_a_plain_http_error_fails_one_plant_only(app: Conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "29.74500" in request.url.params["data"]:
            return httpx.Response(404)
        return httpx.Response(200, content=EMPTY)

    fetcher, _, _ = make_fetcher(handler)
    outcome = go(app, fetcher, FakeStore(), [R1, R2])
    assert outcome.status == "failed"
    assert outcome.note == "R9001: fetch http_status"
    assert count(app, "plant_footprint") == 1


def test_a_missing_named_ref_fails_that_plant(app: Conn) -> None:
    named = replace(R1, osm_ref="way/42")
    fetcher, net, _ = make_fetcher(by_plant({}))
    outcome = go(app, fetcher, FakeStore(), [named])
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert "way/42 is not in" in outcome.note
    assert "way(42)" in net.requests[0].url.params["data"]
