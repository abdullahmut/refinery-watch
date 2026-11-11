import json
from collections.abc import Iterator

import httpx
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, Handler, make_fetcher, sample_bytes

from refinery_watch.db.connect import Conn
from refinery_watch.sources.eia_wpsr import run
from refinery_watch.sources.http import Fetcher
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = pytest.mark.integration

FEED_URL = "https://eia.example.com/v2/petroleum/pnp/wiup/data/"
KEY = "HANDMADEKEY123"


def reply(body: bytes) -> Handler:
    return lambda request: httpx.Response(200, content=body)


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def go(conn: Conn, fetcher: Fetcher, store: FakeStore) -> RunOutcome:
    row = conn.execute("INSERT INTO job_run (job) VALUES ('fetch_eia_wpsr') RETURNING id")
    found = row.fetchone()
    assert found is not None
    conn.commit()
    run_id = int(found["id"])
    outcome = run(conn, fetcher, store, run_id, FEED_URL, KEY, float("inf"))
    finish_run(conn, run_id, outcome)
    return outcome


def test_prices_are_written_and_linked_to_the_document(app: Conn) -> None:
    fetcher, net, _ = make_fetcher(reply(sample_bytes("eia_v2_data_handmade.json")))
    store = FakeStore()
    outcome = go(app, fetcher, store)
    assert (outcome.status, outcome.rows_in, outcome.rows_out) == ("ok", 3, 3)
    doc = app.execute("SELECT * FROM raw_document").fetchone()
    assert doc is not None
    assert doc["source_id"] == "eia_wpsr"
    assert doc["prefilter_passed"] is False
    assert doc["published_at"] is None
    assert doc["object_key"].endswith(".json")
    assert doc["url"] == FEED_URL
    prices = app.execute("SELECT * FROM price ORDER BY series, observed_on").fetchall()
    assert len(prices) == 3
    assert {p["raw_document_id"] for p in prices} == {doc["id"]}
    assert {p["source_id"] for p in prices} == {"eia_wpsr"}
    assert prices[0]["series"] == "eia.testseries1"
    assert net.requests[0].url.params["api_key"] == KEY


def test_a_second_run_with_the_same_data_writes_nothing(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(reply(sample_bytes("eia_v2_data_handmade.json")))
    store = FakeStore()
    go(app, fetcher, store)
    outcome = go(app, fetcher, store)
    assert (outcome.status, outcome.rows_out, outcome.note) == ("ok", 0, "unchanged")
    for table in ("raw_document", "price"):
        count = app.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        assert count is not None
        assert count["n"] == (1 if table == "raw_document" else 3)
    assert len(store.objects) == 1


def test_a_revised_value_adds_a_document_and_one_price(app: Conn) -> None:
    body = json.loads(sample_bytes("eia_v2_data_handmade.json"))
    first, _, _ = make_fetcher(reply(json.dumps(body).encode()))
    go(app, first, FakeStore())
    body["response"]["data"][0]["value"] = "90.4"
    second, _, _ = make_fetcher(reply(json.dumps(body).encode()))
    outcome = go(app, second, FakeStore())
    assert outcome.rows_out == 1
    count = app.execute("SELECT count(*) AS n FROM price").fetchone()
    assert count is not None
    assert count["n"] == 4


def test_the_key_is_never_stored_or_logged(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(reply(sample_bytes("eia_v2_data_handmade.json")))
    store = FakeStore()
    go(app, fetcher, store)
    (stored,) = store.objects.values()
    assert KEY.encode() not in stored
    assert b"REDACTED" in stored
    err = app.execute("SELECT error FROM job_run").fetchone()
    assert err is not None
    assert KEY not in (err["error"] or "")


def test_an_error_reply_fails_the_run_without_the_key(app: Conn) -> None:
    body = json.dumps({"error": f"bad key {KEY}", "code": 403}).encode()
    fetcher, _, _ = make_fetcher(reply(body))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert KEY not in outcome.note
    row = app.execute("SELECT status, error FROM job_run").fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert KEY not in row["error"]


def test_no_allowlisted_series_fails_the_run(app: Conn) -> None:
    body = b'{"response": {"data": [{"period": "2026-11-06", "series": "OTHER", "value": "1"}]}}'
    fetcher, _, _ = make_fetcher(reply(body))
    outcome = go(app, fetcher, FakeStore())
    assert (outcome.status, outcome.note) == ("failed", "no allowlisted series")


def test_http_failure_text_has_no_query(app: Conn) -> None:
    fetcher, _, _ = make_fetcher(lambda request: httpx.Response(403))
    outcome = go(app, fetcher, FakeStore())
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert KEY not in outcome.note
    assert "?" not in outcome.note
