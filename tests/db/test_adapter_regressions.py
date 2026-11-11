"""Regression tests for defects found after the first adapters build."""

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, make_fetcher, sample_bytes

from refinery_watch.db.connect import Conn
from refinery_watch.sources import eia_wpsr
from refinery_watch.sources.store import RunOutcome, finish_run

pytestmark = pytest.mark.integration

EIA_URL = "https://eia.example.com/v2/petroleum/pnp/wiup/data/"


@pytest.fixture
def app(seeded_schema: str, app_url: str) -> Iterator[Conn]:
    conn = open_in_schema(app_url, seeded_schema)
    yield conn
    conn.rollback()
    conn.close()


def new_run(conn: Conn, job: str) -> int:
    row = conn.execute("INSERT INTO job_run (job) VALUES (%s) RETURNING id", (job,)).fetchone()
    assert row is not None
    conn.commit()
    return int(row["id"])


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def test_eia_document_and_prices_are_one_transaction(
    app: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = sample_bytes("eia_v2_data_handmade.json")

    def broken(*args: object, **kwargs: object) -> int:
        raise psycopg.errors.QueryCanceled("statement timeout")

    monkeypatch.setattr("refinery_watch.sources.eia_wpsr.write_prices", broken)
    first, _, _ = make_fetcher(lambda r: httpx.Response(200, content=body))
    run1 = new_run(app, "fetch_eia_wpsr")
    with pytest.raises(psycopg.errors.QueryCanceled):
        eia_wpsr.run(app, first, FakeStore(), run1, EIA_URL, "K")
    finish_run(app, run1, RunOutcome("failed", 0, 0, "QueryCanceled"))
    assert count(app, "raw_document") == 0
    monkeypatch.undo()
    second, _, _ = make_fetcher(lambda r: httpx.Response(200, content=body))
    outcome = eia_wpsr.run(app, second, FakeStore(), new_run(app, "fetch_eia_wpsr"), EIA_URL, "K")
    assert outcome.rows_out == 3
    assert count(app, "price") == 3
    assert count(app, "raw_document") == 1
