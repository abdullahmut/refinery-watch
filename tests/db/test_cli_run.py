from urllib.parse import quote

import httpx
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore, Handler, Net, html_response

from refinery_watch.sources import __main__ as cli
from refinery_watch.sources import hp_rss
from refinery_watch.sources.http import FetchError

pytestmark = pytest.mark.integration

FEED = (
    b"<rss version='2.0'><channel><item><title>Testville Refinery crude unit shut</title>"
    b"<link>https://hp.example.com/news/a</link></item></channel></rss>"
)


def site() -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(200, content=FEED)
        if request.url.path == "/news/a":
            return html_response("article_handmade.html")
        return httpx.Response(404)

    return handler


def transport() -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return site()(request)

    return httpx.MockTransport(respond)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, seeded_schema: str, app_url: str) -> pytest.MonkeyPatch:
    options = quote(f"-c search_path={seeded_schema}")
    monkeypatch.setenv("DATABASE_URL", f"{app_url}?options={options}")
    monkeypatch.setenv("CONTACT_EMAIL", "contact@example.com")
    monkeypatch.setenv("OBJECT_STORE_ENDPOINT", "https://store.example.com")
    monkeypatch.setenv("OBJECT_STORE_REGION", "test-1")
    monkeypatch.setenv("OBJECT_STORE_BUCKET_RAW", "rw-raw-test")
    monkeypatch.setenv("OBJECT_STORE_ACCESS_KEY", "access-id")
    monkeypatch.setenv("OBJECT_STORE_SECRET_KEY", "secret-value-xyz")
    return monkeypatch


def public(host: str) -> list[str]:
    return ["93.184.216.34"]


def rows(schema: str, owner_url: str, sql: str) -> list[dict[str, object]]:
    with open_in_schema(owner_url, schema) as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def test_hp_news_runs_end_to_end(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore()
    code = cli.main(["hp_news"], transport=transport(), resolve=public, store=store)
    assert code == 0
    runs = rows(seeded_schema, pg_server, "SELECT status, rows_in, rows_out FROM job_run")
    assert runs == [{"status": "ok", "rows_in": 1, "rows_out": 1}]
    docs = rows(
        seeded_schema,
        pg_server,
        "SELECT source_id, prefilter_passed, prefilter_terms FROM raw_document",
    )
    assert docs[0]["source_id"] == "hp_news"
    assert docs[0]["prefilter_passed"] is True
    assert "refinery:R9001" in str(docs[0]["prefilter_terms"])
    assert len(store.objects) == 1
    assert "job=fetch_hp_news status=ok rows_in=1 rows_out=1" in capsys.readouterr().out


def test_second_call_at_once_is_skipped_with_exit_0(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str
) -> None:
    cli.main(["hp_news"], transport=transport(), resolve=public, store=FakeStore())
    code = cli.main(["hp_news"], transport=transport(), resolve=public, store=FakeStore())
    assert code == 0
    runs = rows(seeded_schema, pg_server, "SELECT status, error FROM job_run ORDER BY id")
    assert [r["status"] for r in runs] == ["ok", "skipped"]
    assert runs[1]["error"] == "interval"


def test_missing_source_exits_1_with_the_message(
    env: pytest.MonkeyPatch,
    fresh_schema: str,
    app_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    options = quote(f"-c search_path={fresh_schema}")
    env.setenv("DATABASE_URL", f"{app_url}?options={options}")
    code = cli.main(["gdelt_doc"], transport=transport(), resolve=public, store=FakeStore())
    assert code == 1
    assert "source gdelt_doc not loaded; run the source seed loader" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["unchecked", "robots_refused_fetcher_must_verify"])
def test_source_without_a_clear_robots_status_is_not_fetched(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str, status: str
) -> None:
    with open_in_schema(pg_server, seeded_schema) as conn:
        conn.execute("UPDATE source SET robots_status = %s WHERE id = 'hp_news'", (status,))
        conn.commit()
    net = Net(site(), lambda r: httpx.Response(404))
    code = cli.main(
        ["hp_news"], transport=httpx.MockTransport(net), resolve=public, store=FakeStore()
    )
    assert code == 1
    assert net.requests == []
    assert net.robots_requests == []
    assert rows(seeded_schema, pg_server, "SELECT id FROM job_run") == []


def test_unexpected_error_still_finishes_the_run(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret detail")

    env.setattr(hp_rss, "run", boom)
    code = cli.main(["hp_news"], transport=transport(), resolve=public, store=FakeStore())
    assert code == 1
    (run,) = rows(seeded_schema, pg_server, "SELECT status, error, finished_at FROM job_run")
    assert run["status"] == "failed"
    assert run["error"] == "RuntimeError"
    assert run["finished_at"] is not None


def test_fetch_error_text_is_kept(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise FetchError("http_status", "https://hp.example.com/x?k=1", 500)

    env.setattr(hp_rss, "run", blocked)
    assert cli.main(["hp_news"], transport=transport(), resolve=public, store=FakeStore()) == 1
    (run,) = rows(seeded_schema, pg_server, "SELECT error FROM job_run")
    assert run["error"] == "FetchError: http_status host=hp.example.com path=/x"


def test_the_failed_outcome_gives_exit_1(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    t = httpx.MockTransport(
        lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else forbidden(r)
    )
    assert cli.main(["hp_news"], transport=t, resolve=public, store=FakeStore()) == 1
    (run,) = rows(seeded_schema, pg_server, "SELECT status FROM job_run")
    assert run["status"] == "failed"


def test_eia_runs_through_the_cli(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str
) -> None:
    from tests.sources.helpers import sample_bytes

    env.setenv("EIA_API_KEY", "HANDMADEKEY123")
    t = httpx.MockTransport(
        lambda r: (
            httpx.Response(404)
            if r.url.path == "/robots.txt"
            else httpx.Response(200, content=sample_bytes("eia_v2_data_handmade.json"))
        )
    )
    store = FakeStore()
    assert cli.main(["eia_wpsr"], transport=t, resolve=public, store=store) == 0
    assert len(rows(seeded_schema, pg_server, "SELECT id FROM price")) == 3
    assert all(b"HANDMADEKEY123" not in data for data in store.objects.values())
