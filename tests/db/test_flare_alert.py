import logging
import math
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tests.db.flare_helpers import add_day, add_footprint, table_count
from tests.db.flare_helpers import no_network as no_network
from tests.db.helpers import add_document, insert_raw_outage, open_in_schema
from tests.mail.fake_resend import FakeMailer, failed

from refinery_watch.db.connect import Conn
from refinery_watch.flare.alert import (
    AlertSettings,
    RecipientsChangedError,
    _claim,
    candidates,
    claim,
    deliver,
    pending_retries,
    render,
    render_all,
    run_alerts,
)
from refinery_watch.mail.resend import Email, SendResult

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_network")]

TODAY = datetime.now(UTC).date()
SETTINGS = AlertSettings("Desk <desk@example.com>", ("one@example.com", "two@example.com"), 10)
OTHER = AlertSettings("Desk <desk@example.com>", ("three@example.com",), 10)


def now() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=5)


@dataclass
class Db:
    owner: Conn
    app: Conn


@pytest.fixture
def db2(seeded_schema: str, pg_server: str, app_url: str) -> Iterator[Db]:
    owner = open_in_schema(pg_server, seeded_schema)
    app = open_in_schema(app_url, seeded_schema)
    yield Db(owner, app)
    for conn in (app, owner):
        conn.rollback()
        conn.close()


def hot_day(
    conn: Conn,
    refinery_id: str = "R9001",
    day: date = TODAY,
    z: float = 7.5,
    n: int = 3,
    footprint: int | None = None,
    **over: object,
) -> int:
    fp = footprint if footprint is not None else add_footprint(conn, refinery_id)
    fields: dict[str, object] = {
        "n_detections": n,
        "frp_sum_mw": 99,
        "x": math.log(100),
        "baseline_median": math.log(4),
        "baseline_mad": 0.3,
        "z": z,
    }
    fields.update(over)
    return add_day(conn, fp, day, refinery_id=refinery_id, **fields)


def commit_hot(db: Db, **over: object) -> int:
    day = hot_day(db.owner, **over)  # type: ignore[arg-type]
    db.owner.commit()
    return day


def alerts(conn: Conn) -> list[dict[str, object]]:
    return [dict(r) for r in conn.execute("SELECT * FROM flare_alert ORDER BY id").fetchall()]


def attempts(conn: Conn) -> list[dict[str, object]]:
    rows = conn.execute("SELECT * FROM flare_alert_attempt ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def test_a_live_row_at_the_threshold_is_a_candidate_with_its_figures(db2: Db) -> None:
    commit_hot(db2)
    (c,) = candidates(db2.app, now())
    assert (c.refinery_id, c.name, c.country, c.region) == (
        "R9001",
        "Testville Refinery",
        "United States",
        "US Gulf",
    )
    assert c.flare_date == TODAY
    assert (c.n_detections, c.frp_sum_mw) == (3, Decimal("99.0"))
    assert (c.median_frp_mw, c.ratio, c.z, c.baseline_days) == (
        Decimal("3.0"),
        Decimal("25.0"),
        Decimal("7.50"),
        90,
    )
    assert (c.z_min, c.threshold_method) == (Decimal("6.00"), "default")
    assert (c.footprint_kind, c.radius_m) == ("point_radius", 2500)
    assert c.evidence == ()


def test_the_ratio_is_none_for_a_plant_that_is_usually_dark(db2: Db) -> None:
    commit_hot(db2, baseline_median=0.0, baseline_mad=0.0)
    (c,) = candidates(db2.app, now())
    assert (c.median_frp_mw, c.ratio) == (Decimal("0.0"), None)


def test_yesterday_counts_and_three_days_ago_does_not(db2: Db) -> None:
    commit_hot(db2, day=TODAY - timedelta(days=1))
    assert len(candidates(db2.app, now())) == 1
    hot_day(db2.owner, "R9003", day=TODAY - timedelta(days=3))
    db2.owner.commit()
    assert [c.refinery_id for c in candidates(db2.app, now())] == ["R9001"]


def test_rows_that_do_not_qualify_are_left_out(db2: Db) -> None:
    fp1 = add_footprint(db2.owner, "R9001")
    fp2 = add_footprint(db2.owner, "R9002")
    fp3 = add_footprint(db2.owner, "R9003")
    hot_day(db2.owner, "R9001", footprint=fp1, z=5.9)
    hot_day(db2.owner, "R9002", footprint=fp2, n=1)
    hot_day(
        db2.owner, "R9003", footprint=fp3, backfilled=True, available_at=now() - timedelta(days=1)
    )
    db2.owner.commit()
    assert candidates(db2.app, now()) == []


def test_a_row_without_a_score_is_left_out(db2: Db) -> None:
    commit_hot(db2, baseline_days=30, z=None)
    assert candidates(db2.app, now()) == []


def test_a_superseded_row_is_left_out_and_its_revision_is_used(db2: Db) -> None:
    fp = add_footprint(db2.owner)
    first = hot_day(db2.owner, footprint=fp, z=7.0)
    add_day(
        db2.owner,
        fp,
        TODAY,
        supersedes=first,
        n_detections=4,
        frp_sum_mw=150,
        x=math.log(151),
        baseline_median=math.log(4),
        baseline_mad=0.3,
        z=8.0,
    )
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    assert (c.n_detections, c.z) == (4, Decimal("8.00"))


def test_the_highest_score_comes_first(db2: Db) -> None:
    commit_hot(db2, refinery_id="R9001", z=6.5)
    commit_hot(db2, refinery_id="R9003", z=9.0)
    assert [c.refinery_id for c in candidates(db2.app, now())] == ["R9003", "R9001"]


def test_a_plant_alerted_today_in_dubai_is_left_out(db2: Db) -> None:
    commit_hot(db2)
    (c,) = candidates(db2.app, now())
    subject, text, page = render(c, now())
    email = Email(SETTINGS.sender, SETTINGS.recipients, subject, page, text)
    assert claim(db2.app, c, email, now(), 10) is not None
    db2.app.commit()
    assert candidates(db2.app, now()) == []


def test_a_calibrated_alert_threshold_replaces_the_default(db2: Db) -> None:
    commit_hot(db2, z=7.5)
    db2.owner.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('alert', 8.0, 2, 'calibrated')"
    )
    db2.owner.commit()
    assert candidates(db2.app, now()) == []
    db2.owner.execute(
        "INSERT INTO flare_threshold (kind, z_min, min_detections, method)"
        " VALUES ('alert', 7.0, 2, 'calibrated')"
    )
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    assert (c.z_min, c.threshold_method) == (Decimal("7.00"), "calibrated")


def test_candidates_carry_the_evidence_known_by_then(db2: Db) -> None:
    commit_hot(db2)
    event = insert_raw_outage(
        db2.owner,
        start_date=TODAY,
        known_at=datetime.now(UTC) - timedelta(days=1),
        unit_label="CDU",
    )
    db2.owner.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
        " VALUES (%s, %s, 'primary', 'other_media', 'text')",
        (event, add_document(db2.owner, 9001)),
    )
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    assert [(e.kind, e.ref_id) for e in c.evidence] == [("outage_near", event)]
    _, text, _ = render(c, now())
    assert "News or regulator item: yes." in text
    assert "Outage near the date: unplanned at CDU" in text
    assert "confidence high" in text


def test_a_changed_sql_result_changes_the_text(db2: Db) -> None:
    fp = add_footprint(db2.owner)
    first = hot_day(db2.owner, footprint=fp, z=7.0)
    db2.owner.commit()
    (before,) = candidates(db2.app, now())
    add_day(
        db2.owner,
        fp,
        TODAY,
        supersedes=first,
        n_detections=6,
        frp_sum_mw=250,
        x=math.log(251),
        baseline_median=math.log(4),
        baseline_mad=0.3,
        z=9.25,
    )
    db2.owner.commit()
    (after,) = candidates(db2.app, now())
    text_before = render(before, now())[1]
    text_after = render(after, now())[1]
    assert "detections on" in text_before
    assert "(UTC day): 3," in text_before
    assert "(UTC day): 6," in text_after
    assert "power 250.0 MW" in text_after
    assert "Score 9.25," in text_after
    assert "Score 7.00," in text_before


def test_a_dry_run_writes_nothing_and_sends_nothing(db2: Db) -> None:
    commit_hot(db2)
    result = run_alerts(db2.app, None, SETTINGS, now())
    assert result == [("R9001", "dry_run")]
    assert table_count(db2.app, "flare_alert") == 0
    assert table_count(db2.app, "flare_alert_attempt") == 0
    lines = render_all(db2.app, now())
    assert [r[0] for r in lines] == ["R9001"]
    assert lines[0][1].startswith("Flaring alert: Testville Refinery")


def test_one_claim_one_sent_attempt_and_a_second_run_sends_nothing(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer()
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == [("R9001", "sent")]
    assert len(mailer.sent) == 1
    email, key = mailer.sent[0]
    assert email.sender == SETTINGS.sender
    assert email.to == SETTINGS.recipients
    assert email.subject.startswith("Flaring alert: Testville Refinery")
    assert len(key) == 32
    (alert,) = alerts(db2.app)
    assert alert["idempotency_key"] == key
    assert alert["evidence_found"] is False
    assert alert["subject"] == email.subject
    assert "@" not in str(alert["body_text"]) + str(alert["body_html"]) + str(alert["subject"])
    (attempt,) = attempts(db2.app)
    assert (attempt["attempt"], attempt["status"], attempt["provider_message_id"]) == (
        1,
        "sent",
        "msg_0001",
    )
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == []
    assert len(mailer.sent) == 1


def test_the_stored_figures_are_the_candidate_numbers(db2: Db) -> None:
    commit_hot(db2)
    run_alerts(db2.app, FakeMailer(), SETTINGS, now())
    (alert,) = alerts(db2.app)
    figures = alert["figures"]
    assert isinstance(figures, dict)
    assert figures["z"] == "7.50"
    assert figures["ratio"] == "25.0"
    assert figures["n_detections"] == 3
    assert figures["threshold_method"] == "default"


def test_three_failures_then_no_fourth_attempt(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed(500, "internal_server_error")])
    for _ in range(3):
        run_alerts(db2.app, mailer, SETTINGS, now())
    assert len(mailer.sent) == 3
    assert len({key for _, key in mailer.sent}) == 1
    assert len({email for email, _ in mailer.sent}) == 1
    rows = attempts(db2.app)
    assert [(r["attempt"], r["status"]) for r in rows] == [
        (1, "send_failed"),
        (2, "send_failed"),
        (3, "send_failed"),
    ]
    assert rows[0]["error"] == "500 internal_server_error"
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == []
    assert len(mailer.sent) == 3
    assert pending_retries(db2.app, now()) == []


def test_a_failure_then_success_ends_the_retries(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed(), SendResult(True, "m9", 200, None, 1)])
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == [("R9001", "send_failed")]
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == [("R9001", "sent")]
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == []
    assert len(mailer.sent) == 2


def test_a_crash_after_the_send_resends_the_same_bytes_with_the_same_key(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer()
    calls = {"n": 0}

    def crash_once(email: Email, key: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process died after the send")

    mailer.on_send = crash_once
    with pytest.raises(RuntimeError, match="died"):
        run_alerts(db2.app, mailer, SETTINGS, now())
    db2.app.rollback()
    assert len(alerts(db2.app)) == 1
    assert attempts(db2.app) == []
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == [("R9001", "sent")]
    assert len(mailer.sent) == 2
    assert mailer.sent[0] == mailer.sent[1]
    assert [r["status"] for r in attempts(db2.app)] == ["sent"]


def test_eleven_candidates_with_a_cap_of_ten(db2: Db) -> None:
    ids = [f"R91{n:02d}" for n in range(11)]
    for refinery_id in ids:
        db2.owner.execute(
            "INSERT INTO refinery (id, name, country, region, status)"
            " VALUES (%s, %s, 'United States', 'US Gulf', 'operating')",
            (refinery_id, f"Plant {refinery_id}"),
        )
        hot_day(db2.owner, refinery_id, z=7.0 + int(refinery_id[-2:]) / 10)
    db2.owner.commit()
    mailer = FakeMailer()
    settings = AlertSettings(SETTINGS.sender, SETTINGS.recipients, 10)
    result = run_alerts(db2.app, mailer, settings, now())
    assert [o for _, o in result].count("sent") == 10
    assert [o for _, o in result].count("capped") == 1
    assert result[-1] == ("R9100", "capped")
    assert len(mailer.sent) == 10
    assert table_count(db2.app, "flare_alert") == 10
    again = run_alerts(db2.app, mailer, settings, now())
    assert again == [("R9100", "capped")]
    assert len(mailer.sent) == 10


def test_two_concurrent_claims_for_one_plant_make_one_row(
    db2: Db, seeded_schema: str, app_url: str
) -> None:
    commit_hot(db2)
    (c,) = candidates(db2.app, now())
    db2.app.rollback()
    subject, text, page = render(c, now())
    email = Email(SETTINGS.sender, SETTINGS.recipients, subject, page, text)
    results: list[int | None] = []
    gate = threading.Barrier(2)

    def worker() -> None:
        conn = open_in_schema(app_url, seeded_schema)
        try:
            gate.wait(timeout=10)
            results.append(claim(conn, c, email, now(), 10))
            conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(r is None for r in results) == [False, True]
    assert table_count(db2.app, "flare_alert") == 1


def test_claim_returns_none_when_capped_or_already_claimed(db2: Db) -> None:
    commit_hot(db2)
    (c,) = candidates(db2.app, now())
    subject, text, page = render(c, now())
    email = Email(SETTINGS.sender, SETTINGS.recipients, subject, page, text)
    assert claim(db2.app, c, email, now(), 0) is None
    first = claim(db2.app, c, email, now(), 10)
    assert first is not None
    assert claim(db2.app, c, email, now(), 10) is None
    assert table_count(db2.app, "flare_alert") == 1


def test_a_changed_recipient_list_refuses_a_retry(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed()])
    run_alerts(db2.app, mailer, SETTINGS, now())
    (alert_id,) = pending_retries(db2.app, now())
    with pytest.raises(RecipientsChangedError):
        deliver(db2.app, alert_id, mailer, OTHER)
    assert run_alerts(db2.app, mailer, OTHER, now()) == [("R9001", "send_failed")]
    assert len(mailer.sent) == 1
    assert len(attempts(db2.app)) == 1


def test_a_changed_sender_also_refuses(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed()])
    run_alerts(db2.app, mailer, SETTINGS, now())
    (alert_id,) = pending_retries(db2.app, now())
    moved = AlertSettings("Other <o@example.com>", SETTINGS.recipients, 10)
    with pytest.raises(RecipientsChangedError):
        deliver(db2.app, alert_id, mailer, moved)


def test_deliver_on_a_sent_alert_does_not_send_again(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer()
    run_alerts(db2.app, mailer, SETTINGS, now())
    alert_id = int(alerts(db2.app)[0]["id"])  # type: ignore[call-overload]
    assert deliver(db2.app, alert_id, mailer, SETTINGS) == "sent"
    assert len(mailer.sent) == 1


def test_deliver_for_a_missing_alert_raises(db2: Db) -> None:
    with pytest.raises(LookupError):
        deliver(db2.app, 999_999, FakeMailer(), SETTINGS)


def test_an_alert_older_than_eighteen_hours_is_not_retried(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed()])
    run_alerts(db2.app, mailer, SETTINGS, now())
    later = now() + timedelta(hours=19)
    assert pending_retries(db2.app, later) == []
    (alert_id,) = pending_retries(db2.app, now())
    assert pending_retries(db2.app, now() + timedelta(hours=17)) == [alert_id]


def test_a_plant_that_stays_hot_is_not_alerted_twice_on_one_day(db2: Db) -> None:
    fp = add_footprint(db2.owner)
    first = hot_day(db2.owner, footprint=fp)
    db2.owner.commit()
    mailer = FakeMailer()
    run_alerts(db2.app, mailer, SETTINGS, now())
    add_day(
        db2.owner,
        fp,
        TODAY,
        supersedes=first,
        n_detections=9,
        frp_sum_mw=400,
        x=math.log(401),
        baseline_median=math.log(4),
        baseline_mad=0.3,
        z=11.0,
    )
    db2.owner.commit()
    assert run_alerts(db2.app, mailer, SETTINGS, now()) == []
    assert len(mailer.sent) == 1


def test_run_alerts_refuses_a_naive_time(db2: Db) -> None:
    with pytest.raises(ValueError, match="aware"):
        run_alerts(db2.app, None, SETTINGS, datetime(2026, 11, 12))  # noqa: DTZ001


def test_the_database_error_for_a_stale_day_is_not_a_crash(db2: Db) -> None:
    fp = add_footprint(db2.owner)
    first = hot_day(db2.owner, footprint=fp)
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    db2.app.rollback()
    add_day(
        db2.owner,
        fp,
        TODAY,
        supersedes=first,
        n_detections=4,
        frp_sum_mw=120,
        x=math.log(121),
        baseline_median=math.log(4),
        baseline_mad=0.3,
        z=8.0,
    )
    db2.owner.commit()
    subject, text, page = render(c, now())
    email = Email(SETTINGS.sender, SETTINGS.recipients, subject, page, text)
    assert claim(db2.app, c, email, now(), 10) is None
    assert table_count(db2.app, "flare_alert") == 0


def _unsourced_outage(db: Db, **over: object) -> None:
    fields: dict[str, object] = {
        "start_date": TODAY,
        "known_at": datetime.now(UTC) - timedelta(days=1),
        "refinery_unit_id": None,
        "whole_plant": False,
        "unit_label": None,
        "capacity_basis": "not_stated",
        "capacity_affected_kbd": None,
        "confidence": "low",
    }
    fields.update(over)
    event = insert_raw_outage(db.owner, **fields)
    db.owner.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
        " VALUES (%s, %s, 'primary', 'other_media', 'text')",
        (event, add_document(db.owner, 9001)),
    )
    db.owner.commit()


def test_an_outage_with_no_unit_named_is_not_called_whole_plant(db2: Db) -> None:
    commit_hot(db2)
    _unsourced_outage(db2)
    (c,) = candidates(db2.app, now())
    _, text, _ = render(c, now())
    assert "Outage near the date: unplanned at unit not stated," in text
    assert "whole plant" not in text


def test_a_whole_plant_outage_is_called_whole_plant(db2: Db) -> None:
    commit_hot(db2)
    _unsourced_outage(
        db2, whole_plant=True, capacity_basis="plant_nameplate", capacity_affected_kbd=200
    )
    (c,) = candidates(db2.app, now())
    assert c.evidence[0].whole_plant is True
    _, text, _ = render(c, now())
    assert "unplanned at whole plant," in text


def test_a_median_between_half_and_one_megawatt_has_no_ratio(db2: Db) -> None:
    commit_hot(db2, baseline_median=math.log(1.7))
    (c,) = candidates(db2.app, now())
    assert (c.median_frp_mw, c.ratio) == (Decimal("0.7"), None)
    _, text, _ = render(c, now())
    assert "Its normal is under 1 MW a day." in text
    assert "usually shows no detections" not in text


def test_two_days_ago_is_not_a_candidate(db2: Db) -> None:
    hot_day(db2.owner, day=TODAY - timedelta(days=2))
    db2.owner.commit()
    assert candidates(db2.app, now()) == []


def _mail(c: object) -> Email:
    subject, text, page = render(c, now())  # type: ignore[arg-type]
    return Email(SETTINGS.sender, SETTINGS.recipients, subject, page, text)


def test_a_plant_alerted_today_for_yesterday_is_not_a_candidate_for_today(db2: Db) -> None:
    fp = add_footprint(db2.owner)
    hot_day(db2.owner, day=TODAY - timedelta(days=1), footprint=fp)
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    assert claim(db2.app, c, _mail(c), now(), 10) is not None
    db2.app.commit()
    hot_day(db2.owner, day=TODAY, footprint=fp)
    db2.owner.commit()
    assert candidates(db2.app, now()) == []


def test_the_daily_cap_holds_for_two_plants_claimed_at_once(
    db2: Db, seeded_schema: str, app_url: str
) -> None:
    hot_day(db2.owner, "R9001")
    hot_day(db2.owner, "R9003")
    db2.owner.commit()
    found = {c.refinery_id: c for c in candidates(db2.app, now())}
    db2.app.rollback()
    other = open_in_schema(app_url, seeded_schema)
    try:
        db2.app.execute("SELECT 1")  # an open transaction keeps the claim lock until commit
        first = claim(db2.app, found["R9001"], _mail(found["R9001"]), now(), 1)
        result: list[int | None] = []
        worker = threading.Thread(
            target=lambda: result.append(
                claim(other, found["R9003"], _mail(found["R9003"]), now(), 1)
            )
        )
        worker.start()
        worker.join(1.0)
        db2.app.commit()
        worker.join(10.0)
        other.commit()
        assert first is not None
        assert result == [None]
    finally:
        other.close()


def test_deliver_after_three_attempts_sends_nothing(db2: Db) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed()])
    for _ in range(3):
        run_alerts(db2.app, mailer, SETTINGS, now())
    row = db2.app.execute("SELECT id FROM flare_alert").fetchone()
    assert row is not None
    db2.app.commit()
    assert deliver(db2.app, row["id"], mailer, SETTINGS) == "send_failed"
    assert len(mailer.sent) == 3


def test_a_stale_day_at_claim_time_has_its_own_outcome(db2: Db) -> None:
    fp = add_footprint(db2.owner)
    first = hot_day(db2.owner, footprint=fp)
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    db2.app.rollback()
    add_day(
        db2.owner,
        fp,
        TODAY,
        supersedes=first,
        n_detections=4,
        frp_sum_mw=120,
        x=math.log(121),
        baseline_median=math.log(4),
        baseline_mad=0.3,
        z=8.0,
    )
    db2.owner.commit()
    assert _claim(db2.app, c, _mail(c), now(), 10) == "stale_day"


def test_a_refused_retry_is_logged_with_the_alert_id(
    db2: Db, caplog: pytest.LogCaptureFixture
) -> None:
    commit_hot(db2)
    mailer = FakeMailer([failed()])
    run_alerts(db2.app, mailer, SETTINGS, now())
    (alert_id,) = pending_retries(db2.app, now())
    caplog.set_level(logging.WARNING, logger="refinery_watch.flare")
    run_alerts(db2.app, mailer, OTHER, now())
    assert f"alert {alert_id}" in caplog.text
    assert "recipients changed" in caplog.text
    assert "three@example.com" not in caplog.text


def test_a_unit_outage_without_a_label_names_its_unit(db2: Db) -> None:
    commit_hot(db2)
    event = insert_raw_outage(
        db2.owner, start_date=TODAY, known_at=datetime.now(UTC) - timedelta(days=1)
    )
    db2.owner.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
        " VALUES (%s, %s, 'primary', 'other_media', 'text')",
        (event, add_document(db2.owner, 9002)),
    )
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    _, text, _ = render(c, now())
    assert "unit not stated" not in text
    assert "unplanned at CDU," in text


def test_a_median_shown_as_one_megawatt_is_not_called_under_one(db2: Db) -> None:
    commit_hot(db2, baseline_median=math.log(1.96))
    (c,) = candidates(db2.app, now())
    _, text, _ = render(c, now())
    assert "median is 1.0 MW" in text
    assert "under 1 MW" not in text
    assert c.ratio is not None


@pytest.mark.xfail(strict=True, reason="open issue: evidence names unit_code only, not unit_tag")
def test_a_tagged_unit_without_a_label_is_named_with_its_tag(db2: Db) -> None:
    commit_hot(db2)
    row = db2.owner.execute(
        "INSERT INTO refinery_unit (refinery_id, unit_code, unit_tag)"
        " VALUES ('R9001', 'CDU', '2') RETURNING id"
    ).fetchone()
    assert row is not None
    event = insert_raw_outage(
        db2.owner,
        start_date=TODAY,
        known_at=datetime.now(UTC) - timedelta(days=1),
        refinery_unit_id=row["id"],
    )
    db2.owner.execute(
        "INSERT INTO event_source (outage_event_id, raw_document_id, role, source_kind, evidence)"
        " VALUES (%s, %s, 'primary', 'other_media', 'text')",
        (event, add_document(db2.owner, 9003)),
    )
    db2.owner.commit()
    (c,) = candidates(db2.app, now())
    _, text, _ = render(c, now())
    assert "at CDU 2," in text
