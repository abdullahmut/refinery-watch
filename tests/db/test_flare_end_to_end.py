"""The whole flare path on invented data: locations to alert, with the outage table untouched."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from tests.db.flare_helpers import FIRMS_KEY, handmade
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import add_document, insert_raw_outage, open_in_schema
from tests.mail.fake_resend import FakeMailer
from tests.sources.helpers import FakeStore

from refinery_watch.db.connect import Conn
from refinery_watch.flare import __main__ as cli
from refinery_watch.flare.support import confidence_as_of, evidence_as_of

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

HEADER = handmade("firms_area_empty_handmade.csv").decode()
BACKFILL_DAYS = 95
LOCATIONS = (
    "refinery_id,lat,lon,radius_m,osm_ref,source_url,verified\n"
    "R9001,29.745,-95.012,2500,,https://example.test/a,yes\n"
)


def public(host: str) -> list[str]:
    return ["93.184.216.34"]


def detection(day: date, frp: float, minute: int) -> str:
    return (
        f"29.74{minute % 10}00,-95.01100,330.5,0.39,0.36,{day},07{minute:02d},N20,VIIRS,n,"
        f"2.0,295.1,{frp:.2f},N\n"
    )


def area_reply(today: date) -> Callable[[httpx.Request], httpx.Response]:
    """Quiet history: one small detection every tenth day. Yesterday is average; today is hot."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/data_availability/" in path:
            return httpx.Response(200, content=handmade("firms_data_availability_handmade.csv"))
        if "/area/" not in path:
            return httpx.Response(200, content=handmade("overpass_one_way_handmade.json"))
        _, _, _, range_days, start = path.rsplit("/", 4)
        first = date.fromisoformat(start)
        rows = ""
        for offset in range(int(range_days)):
            day = first + timedelta(days=offset)
            if day == today:
                rows += "".join(detection(day, 40.0, m) for m in range(3))
            elif day == today - timedelta(days=1) or day.toordinal() % 10 == 0:
                rows += detection(day, 3.0, 0)
        return httpx.Response(200, content=(HEADER + rows).encode())

    return handler


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, seeded_schema: str, app_url: str, tmp_path: Path
) -> pytest.MonkeyPatch:
    options = quote(f"-c search_path={seeded_schema}")
    monkeypatch.setenv("APP_DATABASE_URL", f"{app_url}?options={options}")
    monkeypatch.setenv("CONTACT_EMAIL", "contact@example.com")
    monkeypatch.setenv("OBJECT_STORE_ENDPOINT", "https://store.example.com")
    monkeypatch.setenv("OBJECT_STORE_REGION", "test-1")
    monkeypatch.setenv("OBJECT_STORE_BUCKET_RAW", "rw-raw-test")
    monkeypatch.setenv("OBJECT_STORE_ACCESS_KEY", "access-id")
    monkeypatch.setenv("OBJECT_STORE_SECRET_KEY", "secret-value-xyz")
    monkeypatch.setenv("FIRMS_MAP_KEY", FIRMS_KEY)
    locations = tmp_path / "locations.csv"
    locations.write_text(LOCATIONS, encoding="utf-8")
    monkeypatch.setenv("REFINERY_LOCATIONS_CSV_PATH", str(locations))
    monkeypatch.setenv("NOTE_RECIPIENTS", "one@example.com")
    monkeypatch.setenv("NOTE_FROM", "Desk <desk@example.com>")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_value")
    monkeypatch.setattr("refinery_watch.sources.http.MIN_HOST_GAP_S", 0.0)
    return monkeypatch


def outage_rows(conn: Conn) -> list[dict[str, object]]:
    return [dict(r) for r in conn.execute("SELECT * FROM outage_event ORDER BY id").fetchall()]


def source_rows(conn: Conn) -> list[dict[str, object]]:
    return [dict(r) for r in conn.execute("SELECT * FROM event_source ORDER BY id").fetchall()]


def test_locations_to_alert_with_the_outage_table_untouched(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    today = now.date()
    last = today - timedelta(days=2)
    first = last - timedelta(days=BACKFILL_DAYS - 1)
    store = FakeStore()
    handler = area_reply(today)

    def transport() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else handler(r)
        )

    with open_in_schema(pg_server, seeded_schema) as owner:
        event = insert_raw_outage(
            owner,
            confidence="low",
            match_score=92,
            capacity_basis="unit_reference",
            start_date=today - timedelta(days=1),
            known_at=now - timedelta(days=3),
            unit_label="CDU",
        )
        owner.execute(
            "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind,"
            " evidence, known_at) VALUES (%s, %s, 'primary', 'other_media', 'text', %s)",
            (event, add_document(owner, 9700), now - timedelta(days=3)),
        )
        owner.commit()
        outages_before = outage_rows(owner)
        sources_before = source_rows(owner)

    kwargs = {"transport": transport(), "resolve": public, "store": store}
    assert cli.main(["footprints"], **kwargs) == 0  # type: ignore[arg-type]
    args = ["backfill", "--from", str(first), "--to", str(last), "--max-requests", "150"]
    assert cli.main(args, **kwargs) == 0  # type: ignore[arg-type]
    assert cli.main(["fetch"], **kwargs) == 0  # type: ignore[arg-type]

    with open_in_schema(pg_server, seeded_schema) as owner:
        kind = owner.execute("SELECT kind FROM plant_footprint").fetchall()
        assert [r["kind"] for r in kind] == ["osm_polygon"]
        days = owner.execute(
            "SELECT day, z, backfilled, supersedes, baseline_days FROM flare_day ORDER BY day, id"
        ).fetchall()
        assert len(days) == BACKFILL_DAYS + 2
        assert [r["backfilled"] for r in days].count(True) == BACKFILL_DAYS
        assert all(r["z"] is None for r in days[:60])
        assert all(r["z"] is not None for r in days[60:])
        live = {r["day"]: r for r in days if not r["backfilled"]}
        assert set(live) == {today - timedelta(days=1), today}
        assert live[today]["z"] > 6.0
        assert 3.5 < live[today - timedelta(days=1)]["z"] < 6.0

        later = datetime.now(UTC) + timedelta(seconds=5)
        (lifted,) = confidence_as_of(owner, later)
        assert (lifted.stored, lifted.effective) == ("low", "medium")
        items = evidence_as_of(owner, "R9001", today, later)
        assert [i.kind for i in items] == ["outage_near"]
        assert items[0].confidence == "medium"

    assert cli.main(["alert"]) == 0
    dry = capsys.readouterr().out
    assert "=== R9001 ===" in dry
    assert "Outage near the date: unplanned at CDU" in dry
    assert "Footprint: OpenStreetMap outline." in dry
    with open_in_schema(pg_server, seeded_schema) as owner:
        assert owner.execute("SELECT count(*) AS n FROM flare_alert").fetchone() == {"n": 0}

    mailer = FakeMailer()
    assert cli.main(["alert", "--send"], mailer=mailer) == 0
    assert len(mailer.sent) == 1
    email, _ = mailer.sent[0]
    assert email.subject.startswith("Flaring alert: Testville Refinery, United States,")
    assert "Outage near the date: unplanned at CDU" in email.text

    with open_in_schema(pg_server, seeded_schema) as owner:
        alert = owner.execute("SELECT evidence_found, flare_date FROM flare_alert").fetchone()
        assert alert == {"evidence_found": True, "flare_date": today}
        assert owner.execute("SELECT status FROM flare_alert_attempt").fetchall() == [
            {"status": "sent"}
        ]
        assert outage_rows(owner) == outages_before
        assert source_rows(owner) == sources_before
