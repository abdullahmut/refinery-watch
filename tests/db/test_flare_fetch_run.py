import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from tests.db.flare_helpers import (
    FIRMS_KEY,
    firms_handler,
    handmade,
    seed_plant,
    start_job,
    table_count,
)
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, make_fetcher

from refinery_watch.db.connect import Conn
from refinery_watch.flare.fetch import fetch_one, live_plans, run_fetch
from refinery_watch.flare.firms import QueryBox, query_boxes
from refinery_watch.flare.footprints import current_footprints
from refinery_watch.flare.geometry import BBox
from refinery_watch.sources.http import Fetcher
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

TODAY = date(2026, 11, 12)
YESTERDAY = date(2026, 11, 11)


@pytest.fixture(autouse=True)
def quiet_http_logs() -> Iterator[None]:
    """The CLI sets these two loggers to WARNING, because httpx logs every URL at INFO."""
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


def go(conn: Conn, fetcher: Fetcher, store: FakeStore, today: date = TODAY) -> RunOutcome:
    run_id = start_job(conn)
    outcome = run_fetch(conn, fetcher, store, run_id, FIRMS_KEY, today, float("inf"))
    finish_run(conn, run_id, outcome)
    return outcome


def days(conn: Conn) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT day, n_detections, frp_sum_mw, products, supersedes, backfilled FROM flare_day"
        " ORDER BY day, id"
    ).fetchall()
    return [dict(r) for r in rows]


def test_a_live_run_writes_detections_coverage_and_two_days(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(firms_handler())
    store = FakeStore()
    outcome = go(app, fetcher, store)
    assert (outcome.status, outcome.rows_in, outcome.rows_out, outcome.note) == ("ok", 2, 4, None)
    assert table_count(app, "flare_detection") == 4
    cover = app.execute(
        "SELECT day, satellite, product FROM flare_coverage ORDER BY day, satellite"
    ).fetchall()
    assert [(r["day"], r["satellite"], r["product"]) for r in cover] == [
        (YESTERDAY, "NOAA20", "NRT"),
        (YESTERDAY, "SNPP", "NRT"),
        (TODAY, "NOAA20", "NRT"),
        (TODAY, "SNPP", "NRT"),
    ]
    rows = days(app)
    assert [(r["day"], r["n_detections"], r["frp_sum_mw"]) for r in rows] == [
        (YESTERDAY, 3, Decimal("20.00")),
        (TODAY, 1, Decimal("8.00")),
    ]
    assert rows[0]["products"] == ["NOAA20:NRT", "SNPP:NRT"]
    assert all(r["backfilled"] is False and r["supersedes"] is None for r in rows)
    assert len(store.objects) == 2
    assert all(key.endswith(".csv") for key in store.objects)
    assert {str(r.url.path).split("/")[5] for r in net.requests} == {
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
    }
    assert all(r.url.path.endswith("/2/2026-11-11") for r in net.requests)


def test_the_detection_outside_the_footprint_is_dropped(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(firms_handler())
    go(app, fetcher, FakeStore())
    lats = app.execute("SELECT latitude FROM flare_detection ORDER BY latitude").fetchall()
    assert Decimal("29.77000") not in [r["latitude"] for r in lats]
    row = app.execute(
        "SELECT satellite, product, confidence, daynight, acquired_at, frp_mw"
        " FROM flare_detection WHERE latitude = 29.746"
    ).fetchone()
    assert row == {
        "satellite": "NOAA20",
        "product": "NRT",
        "confidence": "n",
        "daynight": "N",
        "acquired_at": datetime(2026, 11, 11, 7, 42, tzinfo=UTC),
        "frp_mw": Decimal("10.50"),
    }


def test_a_second_identical_run_writes_nothing(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(firms_handler())
    store = FakeStore()
    go(app, fetcher, store)
    before = {
        t: table_count(app, t)
        for t in ("flare_detection", "flare_coverage", "flare_day", "raw_document")
    }
    outcome = go(app, fetcher, store)
    assert (outcome.status, outcome.rows_out) == ("ok", 0)
    after = {t: table_count(app, t) for t in before}
    assert after == before
    assert len(store.objects) == 2


def test_one_more_detection_for_today_adds_one_superseding_day(app: Conn) -> None:
    first_fetcher, _, _ = make_fetcher(firms_handler())
    go(app, first_fetcher, FakeStore())
    first = app.execute("SELECT id, known_at FROM flare_day WHERE day = %s", (TODAY,)).fetchone()
    assert first is not None
    more = handmade("firms_area_viirs_noaa20_nrt_handmade.csv") + (
        b"29.74700,-95.01300,330.0,0.39,0.36,2026-11-12,0300,N20,VIIRS,h,2.0NRT,296.0,4.50,N\n"
    )
    second_fetcher, _, _ = make_fetcher(firms_handler(noaa20=more))
    outcome = go(app, second_fetcher, FakeStore())
    assert (outcome.status, outcome.rows_out) == ("ok", 1)
    assert table_count(app, "flare_day") == 3
    latest = app.execute("SELECT * FROM flare_day WHERE day = %s ORDER BY id DESC", (TODAY,))
    row = latest.fetchone()
    assert row is not None
    assert (row["supersedes"], row["n_detections"], row["frp_sum_mw"]) == (
        first["id"],
        2,
        Decimal("12.50"),
    )
    assert table_count(app, "flare_coverage") == 4
    then = app.execute(
        "SELECT id FROM flare_day_current_as_of(%s) WHERE day = %s", (first["known_at"], TODAY)
    ).fetchall()
    assert [r["id"] for r in then] == [first["id"]]


def test_one_satellite_failing_for_a_box_gives_no_day_but_other_boxes_score(app: Conn) -> None:
    seed_plant(app, "R9003")
    handler = firms_handler(fail={"VIIRS_NOAA20_NRT/-95.": 404})
    fetcher, _, _ = make_fetcher(handler)
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note == "FetchError: http_status host=firms.modaps.eosdis.nasa.gov status=404"
    assert FIRMS_KEY not in (outcome.note or "")
    plants = {r["refinery_id"] for r in app.execute("SELECT refinery_id FROM flare_day")}
    assert plants == {"R9003"}
    assert table_count(app, "flare_day") == 2
    status = app.execute("SELECT status, error FROM job_run ORDER BY id DESC").fetchone()
    assert status is not None
    assert status["status"] == "failed"
    assert FIRMS_KEY not in status["error"]


def test_a_text_reply_for_a_bad_key_fails_the_plan_and_says_so_without_the_key(app: Conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"Invalid MAP_KEY.")

    fetcher, _, _ = make_fetcher(handler)
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert outcome.note.startswith("FirmsResponseError")
    assert table_count(app, "flare_day") == 0
    assert table_count(app, "raw_document") == 0


def test_a_reply_that_echoes_the_key_is_stored_redacted(app: Conn) -> None:
    body = handmade("firms_area_empty_handmade.csv") + f"# {FIRMS_KEY}\n".encode()
    fetcher, _, _ = make_fetcher(lambda request: httpx.Response(200, content=body))
    store = FakeStore()
    go(app, fetcher, store)
    assert all(FIRMS_KEY.encode() not in data for data in store.objects.values())


@pytest.mark.parametrize("status", [403, 500])
def test_the_key_stays_out_of_every_record_when_the_fetcher_raises(
    app: Conn, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fetcher, net, _ = make_fetcher(lambda request: httpx.Response(status))
    store = FakeStore()
    outcome = go(app, fetcher, store)
    assert outcome.status == "failed"
    assert any(FIRMS_KEY in str(r.url) for r in net.requests)
    assert (outcome.note or "").startswith("HostBlocked")
    stored = [r["error"] for r in app.execute("SELECT error FROM job_run").fetchall()]
    assert all(FIRMS_KEY not in (e or "") for e in stored)
    assert FIRMS_KEY not in caplog.text
    assert store.objects == {}


def test_the_key_stays_out_of_urls_notes_objects_and_logs_on_success(
    app: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fetcher, _, _ = make_fetcher(firms_handler())
    store = FakeStore()
    go(app, fetcher, store)
    urls = [r["url"] for r in app.execute("SELECT url FROM raw_document").fetchall()]
    assert urls
    assert all(FIRMS_KEY not in u and "REDACTED" in u for u in urls)
    assert all(FIRMS_KEY.encode() not in data for data in store.objects.values())
    assert FIRMS_KEY not in caplog.text
    errors = [r["error"] for r in app.execute("SELECT error FROM job_run").fetchall()]
    assert all(FIRMS_KEY not in (e or "") for e in errors)


def test_without_footprints_the_run_fails_with_a_clear_note(
    seeded_schema: str, app_url: str
) -> None:
    conn = open_in_schema(app_url, seeded_schema)
    try:
        fetcher, net, _ = make_fetcher(firms_handler())
        outcome = go(conn, fetcher, FakeStore())
        assert outcome.status == "failed"
        assert outcome.note == "no footprints; run the footprints command first"
        assert net.requests == []
    finally:
        conn.rollback()
        conn.close()


def test_live_plans_cover_both_satellites_yesterday_and_today() -> None:
    box = QueryBox(BBox(-96, 29, -94, 30), ("R9001",))
    plans = live_plans([box], TODAY)
    assert [(p.satellite, p.product, p.start, p.range_days) for p in plans] == [
        ("SNPP", "NRT", YESTERDAY, 2),
        ("NOAA20", "NRT", YESTERDAY, 2),
    ]
    assert plans[0].days() == [YESTERDAY, TODAY]


def test_fetch_one_returns_the_detections_written_and_zero_for_a_known_reply(app: Conn) -> None:
    footprints = current_footprints(app)
    (box,) = query_boxes(footprints)
    plan = live_plans([box], TODAY)[1]
    fetcher, _, _ = make_fetcher(firms_handler())
    run_id = start_job(app)
    store = FakeStore()
    assert fetch_one(app, fetcher, store, run_id, FIRMS_KEY, plan, footprints) == 3
    assert fetch_one(app, fetcher, store, run_id, FIRMS_KEY, plan, footprints) == 0


def test_a_known_reply_does_not_add_coverage_twice(app: Conn) -> None:
    footprints = current_footprints(app)
    (box,) = query_boxes(footprints)
    plan = live_plans([box], TODAY)[1]
    fetcher, _, _ = make_fetcher(firms_handler())
    run_id = start_job(app)
    fetch_one(app, fetcher, FakeStore(), run_id, FIRMS_KEY, plan, footprints)
    fetch_one(app, fetcher, FakeStore(), run_id, FIRMS_KEY, plan, footprints)
    assert table_count(app, "flare_coverage") == 2


def test_a_cross_host_redirect_is_refused_and_not_followed(app: Conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "firms.modaps.eosdis.nasa.gov":
            return httpx.Response(302, headers={"location": "https://other.example.org/steal"})
        return httpx.Response(200, content=b"elsewhere")

    fetcher, net, _ = make_fetcher(handler)
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert outcome.note.startswith("AddressRefused: cross_host_redirect")
    assert FIRMS_KEY not in outcome.note
    assert {r.url.host for r in net.requests} == {"firms.modaps.eosdis.nasa.gov"}
