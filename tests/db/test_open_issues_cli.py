"""Known CLI defect, recorded as a strict expected failure."""

from urllib.parse import quote

import httpx
import pytest
from tests.db.helpers import open_in_schema
from tests.sources.helpers import FakeStore

from refinery_watch.sources import __main__ as cli

pytestmark = pytest.mark.integration


@pytest.mark.xfail(
    strict=True,
    reason="open issue: FETCH_IPV6 is read after start_run, so a bad value leaves a running row",
)
def test_a_bad_fetch_ipv6_exits_before_any_job_run_row(
    monkeypatch: pytest.MonkeyPatch, seeded_schema: str, app_url: str, pg_server: str
) -> None:
    options = quote(f"-c search_path={seeded_schema}")
    monkeypatch.setenv("DATABASE_URL", f"{app_url}?options={options}")
    monkeypatch.setenv("CONTACT_EMAIL", "contact@example.com")
    monkeypatch.setenv("OBJECT_STORE_ENDPOINT", "https://store.example.com")
    monkeypatch.setenv("OBJECT_STORE_REGION", "test-1")
    monkeypatch.setenv("OBJECT_STORE_BUCKET_RAW", "rw-raw-test")
    monkeypatch.setenv("OBJECT_STORE_ACCESS_KEY", "access-id")
    monkeypatch.setenv("OBJECT_STORE_SECRET_KEY", "secret-value-xyz")
    monkeypatch.setenv("FETCH_IPV6", "maybe")
    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    code = cli.main(
        ["hp_news"], transport=transport, resolve=lambda h: ["93.184.216.34"], store=FakeStore()
    )
    with open_in_schema(pg_server, seeded_schema) as conn:
        rows = conn.execute("SELECT status FROM job_run").fetchall()
    assert code == 2
    assert rows == []
