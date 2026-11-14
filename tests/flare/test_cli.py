import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from tests.db.flare_helpers import (
    FIRMS_KEY,
    add_hot_day,
    firms_handler,
    handmade,
    seed_plant,
)
from tests.db.helpers import open_in_schema
from tests.flare.synth import load, quiet_history
from tests.mail.fake_resend import FakeMailer, failed
from tests.sources.helpers import FakeStore

from refinery_watch.flare import __main__ as cli
from refinery_watch.flare.research import NO_HISTORY, RESULTS_NOTE

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)
CSV_HEADER = "refinery_id,lat,lon,radius_m,osm_ref,source_url,verified\n"


def clock() -> datetime:
    return NOW


def public(host: str) -> list[str]:
    return ["93.184.216.34"]


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
    locations.write_text(CSV_HEADER + "R9001,29.745,-95.012,2500,,https://example.test/a,yes\n")
    monkeypatch.setenv("REFINERY_LOCATIONS_CSV_PATH", str(locations))
    monkeypatch.setenv("NOTE_RECIPIENTS", "one@example.com,two@example.com")
    monkeypatch.setenv("NOTE_FROM", "Desk <desk@example.com>")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_value")
    monkeypatch.delenv("FLARE_ALERT_DAILY_MAX", raising=False)
    monkeypatch.delenv("FETCH_IPV6", raising=False)
    monkeypatch.setattr("refinery_watch.sources.http.MIN_HOST_GAP_S", 0.0)
    return monkeypatch


@pytest.fixture
def owner(seeded_schema: str, pg_server: str) -> Callable[[str], list[dict[str, object]]]:
    def query(sql: str) -> list[dict[str, object]]:
        with open_in_schema(pg_server, seeded_schema) as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    return query


def seed(seeded_schema: str, pg_server: str, action: Callable[..., object]) -> None:
    with open_in_schema(pg_server, seeded_schema) as conn:
        action(conn)
        conn.commit()


def transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(request)

    return httpx.MockTransport(respond)


def runs(owner: Callable[[str], list[dict[str, object]]]) -> list[dict[str, object]]:
    return owner("SELECT job, status, rows_in, rows_out, error FROM job_run ORDER BY id")


def test_footprints_runs_end_to_end(
    env: pytest.MonkeyPatch,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    reply = handmade("overpass_one_way_handmade.json")
    store = FakeStore()
    code = cli.main(
        ["footprints"],
        transport=transport(lambda r: httpx.Response(200, content=reply)),
        resolve=public,
        store=store,
        now=clock,
    )
    assert code == 0
    assert runs(owner) == [
        {"job": "fetch_osm_overpass", "status": "ok", "rows_in": 1, "rows_out": 1, "error": None}
    ]
    assert owner("SELECT kind FROM plant_footprint") == [{"kind": "osm_polygon"}]
    assert len(store.objects) == 1
    assert "job=fetch_osm_overpass status=ok rows_in=1 rows_out=1" in capsys.readouterr().out


def test_footprints_max_requests_is_checked(env: pytest.MonkeyPatch) -> None:
    assert cli.main(["footprints", "--max-requests", "0"]) == 2
    assert cli.main(["footprints", "--max-requests", "101"]) == 2
    assert cli.main(["footprints", "--max-requests", "x"]) == 2


def test_a_bad_locations_file_exits_2_and_names_the_line(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(CSV_HEADER + "R9001,95,0,,,,no\n")
    env.setenv("REFINERY_LOCATIONS_CSV_PATH", str(bad))
    assert cli.main(["footprints"], store=FakeStore()) == 2
    assert "line 2" in capsys.readouterr().err


def test_fetch_runs_end_to_end_and_scores_two_days(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(seeded_schema, pg_server, seed_plant)
    store = FakeStore()
    code = cli.main(
        ["fetch"],
        transport=transport(firms_handler()),
        resolve=public,
        store=store,
        now=clock,
    )
    assert code == 0
    assert runs(owner) == [
        {"job": "fetch_nasa_firms", "status": "ok", "rows_in": 2, "rows_out": 4, "error": None}
    ]
    assert owner("SELECT count(*) AS n FROM flare_day") == [{"n": 2}]
    out = capsys.readouterr()
    assert FIRMS_KEY not in out.out + out.err


def test_fetch_failure_exits_1_and_the_key_stays_out_of_output_and_the_job_row(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed(seeded_schema, pg_server, seed_plant)
    caplog.set_level(logging.DEBUG)
    code = cli.main(
        ["fetch"],
        transport=transport(lambda r: httpx.Response(403)),
        resolve=public,
        store=FakeStore(),
        now=clock,
    )
    assert code == 1
    (run,) = runs(owner)
    assert run["status"] == "failed"
    assert FIRMS_KEY not in str(run["error"])
    out = capsys.readouterr()
    assert FIRMS_KEY not in out.out + out.err + caplog.text
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_fetch_refuses_a_source_that_is_not_cleared(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(
        seeded_schema,
        pg_server,
        lambda c: c.execute(
            "UPDATE source SET robots_status = 'unchecked' WHERE id = 'nasa_firms'"
        ),
    )
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    code = cli.main(["fetch"], transport=transport(handler), resolve=public, store=FakeStore())
    assert code == 1
    assert sent == []
    assert "robots status is not a clear allow" in capsys.readouterr().out


def test_fetch_without_the_key_exits_2(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.delenv("FIRMS_MAP_KEY")
    assert cli.main(["fetch"], store=FakeStore()) == 2
    assert "FIRMS_MAP_KEY" in capsys.readouterr().err


def test_footprints_without_the_locations_path_exits_2(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.delenv("REFINERY_LOCATIONS_CSV_PATH")
    assert cli.main(["footprints"], store=FakeStore()) == 2
    assert "REFINERY_LOCATIONS_CSV_PATH" in capsys.readouterr().err


def test_backfill_runs_and_writes_backfilled_days(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
) -> None:
    seed(seeded_schema, pg_server, seed_plant)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/data_availability/" in request.url.path:
            return httpx.Response(200, content=handmade("firms_data_availability_handmade.csv"))
        return httpx.Response(200, content=handmade("firms_area_empty_handmade.csv"))

    code = cli.main(
        ["backfill", "--from", "2026-06-01", "--to", "2026-06-05", "--max-requests", "5"],
        transport=transport(handler),
        resolve=public,
        store=FakeStore(),
        now=clock,
    )
    assert code == 0
    assert runs(owner)[0]["job"] == "flare_backfill"
    days = owner("SELECT backfilled FROM flare_day")
    assert len(days) == 5
    assert all(d["backfilled"] is True for d in days)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["nonsense"],
        ["backfill", "--from", "2026-06-01"],
        ["backfill", "--from", "June", "--to", "2026-06-05"],
        ["backfill", "--from", "2026-06-01", "--to", "2026-06-05", "--max-requests", "0"],
        ["calibrate"],
        ["calibrate", "--kind", "other"],
        ["calibrate", "--kind", "signal", "--target", "-1"],
        ["research"],
        ["research", "--out", "x.md", "--as-of", "2026-11-12T05:00:00"],
        ["research", "--out", "x.md", "--as-of", "soon"],
    ],
)
def test_usage_errors_exit_2(env: pytest.MonkeyPatch, argv: list[str]) -> None:
    assert cli.main(argv) == 2


def test_help_exits_0(env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "footprints" in capsys.readouterr().out


def test_a_missing_database_setting_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("APP_DATABASE_URL", "PG_HOST", "PG_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["alert"]) == 2
    assert "PG_HOST" in capsys.readouterr().err


def test_an_unreachable_database_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "APP_DATABASE_URL", "postgresql://nobody:pw@127.0.0.1:1/none?connect_timeout=2"
    )
    assert cli.main(["alert"]) == 1
    assert "could not connect" in capsys.readouterr().err


def test_alert_without_send_prints_and_writes_nothing(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(seeded_schema, pg_server, lambda c: add_hot_day(c))
    mailer = FakeMailer()
    code = cli.main(["alert"], mailer=mailer)
    out = capsys.readouterr().out
    assert code == 0
    assert "=== R9001 ===" in out
    assert "Subject: Flaring alert: Testville Refinery, United States," in out
    assert "Signal only. Not a confirmed outage" in out
    assert "@" not in out
    assert mailer.sent == []
    assert owner("SELECT count(*) AS n FROM flare_alert") == [{"n": 0}]
    assert runs(owner) == []


def test_alert_dry_run_with_no_candidates(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["alert"]) == 0
    assert "No alert candidates." in capsys.readouterr().out


def test_alert_send_sends_once_and_records_it(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
) -> None:
    seed(seeded_schema, pg_server, lambda c: add_hot_day(c))
    mailer = FakeMailer()
    assert cli.main(["alert", "--send"], mailer=mailer) == 0
    assert len(mailer.sent) == 1
    email, _ = mailer.sent[0]
    assert email.to == ("one@example.com", "two@example.com")
    assert email.sender == "Desk <desk@example.com>"
    assert owner("SELECT status FROM flare_alert_attempt") == [{"status": "sent"}]
    (run,) = runs(owner)
    assert (run["job"], run["status"], run["rows_out"]) == ("flare_alert", "ok", 1)
    assert cli.main(["alert", "--send"], mailer=mailer) == 0
    assert len(mailer.sent) == 1


def test_alert_send_failure_exits_1(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
) -> None:
    seed(seeded_schema, pg_server, lambda c: add_hot_day(c))
    assert cli.main(["alert", "--send"], mailer=FakeMailer([failed()])) == 1
    (run,) = runs(owner)
    assert run["status"] == "failed"
    assert run["error"] == "send failed for R9001"


@pytest.mark.parametrize("name", ["RESEND_API_KEY", "NOTE_FROM", "NOTE_RECIPIENTS"])
def test_alert_send_without_mail_settings_exits_2_and_sends_nothing(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    seed(seeded_schema, pg_server, lambda c: add_hot_day(c))
    env.delenv(name)
    assert cli.main(["alert", "--send"], transport=transport(lambda r: httpx.Response(500))) == 2
    assert name in capsys.readouterr().err
    assert owner("SELECT count(*) AS n FROM flare_alert") == [{"n": 0}]
    assert runs(owner) == []


def test_alert_send_builds_the_real_client_when_no_mailer_is_injected(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
) -> None:
    seed(seeded_schema, pg_server, lambda c: add_hot_day(c))
    # The network guard stops the real client, which proves it was the one used.
    assert cli.main(["alert", "--send"]) == 1
    (run,) = runs(owner)
    assert run["status"] == "failed"
    assert run["error"] == "NetworkBlocked"
    assert owner("SELECT count(*) AS n FROM flare_alert") == [{"n": 1}]
    assert owner("SELECT count(*) AS n FROM flare_alert_attempt") == [{"n": 0}]


def test_calibrate_refuses_with_too_little_history(
    env: pytest.MonkeyPatch,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["calibrate", "--kind", "signal", "--write"], now=clock) == 3
    assert owner("SELECT count(*) AS n FROM flare_threshold") == [{"n": 2}]
    assert "no scored flare days" in runs(owner)[0]["error"]  # type: ignore[operator]


def test_calibrate_prints_the_curve_and_writes_nothing_by_default(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(seeded_schema, pg_server, lambda c: load(c, quiet_history()))
    later = datetime.now(UTC)
    assert cli.main(["calibrate", "--kind", "signal"], now=lambda: later) == 0
    out = capsys.readouterr().out
    assert "z_min  flagged_quiet_days" in out
    assert "Chosen z_min 3.25 for a target of 1.0" in out
    assert owner("SELECT count(*) AS n FROM flare_threshold") == [{"n": 2}]
    assert runs(owner)[0]["status"] == "ok"


def test_calibrate_write_inserts_one_calibrated_row(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(seeded_schema, pg_server, lambda c: load(c, quiet_history()))
    later = datetime.now(UTC)
    args = ["calibrate", "--kind", "alert", "--target", "0.2", "--write"]
    assert cli.main(args, now=lambda: later) == 0
    rows = owner(
        "SELECT kind, z_min, method, min_detections, params FROM flare_threshold ORDER BY id"
    )
    assert len(rows) == 3
    assert rows[-1]["kind"] == "alert"
    assert str(rows[-1]["z_min"]) == "5.25"
    assert (rows[-1]["method"], rows[-1]["min_detections"]) == ("calibrated", 2)
    assert rows[-1]["params"]["target"] == "0.2"  # type: ignore[index]
    assert "Chosen z_min 5.25" in capsys.readouterr().out


def test_calibrate_with_a_zero_target_picks_the_first_value_with_no_false_alarms(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(seeded_schema, pg_server, lambda c: load(c, quiet_history()))
    later = datetime.now(UTC)
    args = ["calibrate", "--kind", "signal", "--target", "0", "--write"]
    assert cli.main(args, now=lambda: later) == 0
    assert owner("SELECT count(*) AS n FROM flare_threshold") == [{"n": 3}]
    assert "Chosen z_min 7.50 for a target of 0" in capsys.readouterr().out


def test_research_writes_the_fixed_text_for_an_empty_history(
    env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "results.md"
    assert cli.main(["research", "--out", str(out)], now=lambda: datetime.now(UTC)) == 0
    assert out.read_text(encoding="utf-8") == NO_HISTORY + "\n\n" + RESULTS_NOTE + "\n"


def test_research_writes_results_for_a_loaded_history(
    env: pytest.MonkeyPatch, seeded_schema: str, pg_server: str, tmp_path: Path
) -> None:
    seed(seeded_schema, pg_server, lambda c: load(c, quiet_history(120)))
    out = tmp_path / "results.md"
    when = datetime.now(UTC).isoformat()
    assert cli.main(["research", "--as-of", when, "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Flare signal results")
    assert "Scored plant-days: 360 at 3 plants" in text
    assert "\r" not in text


def test_research_never_overwrites_the_method_doc(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "flare-lead-time.md"
    target.write_text("method", encoding="utf-8")
    assert cli.main(["research", "--out", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == "method"
    assert "results file only" in capsys.readouterr().err


def test_an_unexpected_error_is_logged_by_type_only(
    env: pytest.MonkeyPatch,
    seeded_schema: str,
    pg_server: str,
    owner: Callable[[str], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(seeded_schema, pg_server, seed_plant)

    class Exploding(FakeStore):
        def put(self, key: str, data: bytes, content_type: str) -> None:
            raise RuntimeError(f"secret detail {FIRMS_KEY}")

    code = cli.main(
        ["fetch"],
        transport=transport(firms_handler()),
        resolve=public,
        store=Exploding(),
        now=clock,
    )
    out = capsys.readouterr()
    assert code == 1
    assert "RuntimeError" in out.out
    assert "secret detail" not in out.out + out.err
    assert FIRMS_KEY not in str(runs(owner))
