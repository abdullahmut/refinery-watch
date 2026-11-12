"""Same-day flaring alerts: candidates, a rendered payload, a claim row, then delivery."""

import html
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import require_aware
from refinery_watch.flare.overpass import FootprintKind
from refinery_watch.flare.support import Evidence, evidence_as_of
from refinery_watch.mail.resend import Email, Mailer, fingerprint

CLAIM_LOCK: Final = 7746021
MAX_ITEMS: Final = 3
MAX_ATTEMPTS: Final = 3
RETRY_WINDOW: Final = timedelta(hours=18)
DUBAI: Final = ZoneInfo("Asia/Dubai")
ITEM_CHARS: Final = 80
FOOTER: Final = (
    "Signal only. Not a confirmed outage and not capacity. Cloud, satellite timing and small"
    " flares can hide flaring. Nearby fires can look like it. Times are Asia/Dubai."
    " Detections: NASA FIRMS VIIRS (Suomi NPP, NOAA-20)."
)
KIND_LABELS: Final = {
    "outage_near": "Outage near the date",
    "outage_open": "Open outage",
    "regulator_near": "Regulator record",
}
_LINE_BREAKS: Final = re.compile(r"\s+")
log = logging.getLogger("refinery_watch.flare")

AlertOutcome = Literal["sent", "send_failed", "capped", "already_claimed", "stale_day", "dry_run"]


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    """Every number here is a column of the candidate query."""

    flare_day_id: int
    refinery_id: str
    name: str
    country: str
    region: str
    flare_date: date
    n_detections: int
    frp_sum_mw: Decimal
    median_frp_mw: Decimal
    ratio: Decimal | None  # None when the median is under 1 MW
    z: Decimal
    baseline_days: int
    z_min: Decimal
    threshold_method: str
    footprint_kind: FootprintKind
    radius_m: int | None
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class AlertSettings:
    sender: str
    recipients: tuple[str, ...]
    daily_max: int


class RecipientsChangedError(RuntimeError):
    """The stored alert was built for another sender or recipient list."""


CANDIDATE_SQL: Final = """
SELECT d.id AS flare_day_id, d.refinery_id, r.name, r.country, r.region, d.day AS flare_date,
       d.n_detections, round(d.frp_sum_mw, 1) AS frp_sum_mw,
       round((exp(d.baseline_median) - 1)::numeric, 1) AS median_frp_mw,
       CASE WHEN exp(d.baseline_median) - 1 < 1 THEN NULL
            ELSE round(exp(d.x - d.baseline_median)::numeric, 1) END AS ratio,
       round(d.z::numeric, 2) AS z, d.baseline_days, t.z_min, t.method AS threshold_method,
       f.kind AS footprint_kind, f.radius_m
FROM flare_day_current_as_of(%(now)s) d
JOIN flare_threshold_as_of('alert', %(now)s) t ON t.id IS NOT NULL
JOIN refinery r ON r.id = d.refinery_id
JOIN plant_footprint f ON f.id = d.footprint_id
WHERE NOT d.backfilled
  AND d.day >= (%(now)s AT TIME ZONE 'UTC')::date - 1
  AND d.z IS NOT NULL AND d.z >= t.z_min AND d.n_detections >= t.min_detections
  AND NOT EXISTS (SELECT 1 FROM flare_alert a
                  WHERE a.refinery_id = d.refinery_id
                    AND (a.flare_date = d.day
                         OR a.alert_day = (%(now)s AT TIME ZONE 'Asia/Dubai')::date))
ORDER BY d.z DESC, d.refinery_id
"""


def candidates(conn: Conn, now: datetime) -> list[AlertCandidate]:
    """Live days at or over the alert threshold that have no alert yet. Highest score first."""
    require_aware(now, "now")
    rows = conn.execute(CANDIDATE_SQL, {"now": now}).fetchall()
    return [
        AlertCandidate(
            flare_day_id=int(r["flare_day_id"]),
            refinery_id=r["refinery_id"],
            name=r["name"],
            country=r["country"],
            region=r["region"],
            flare_date=r["flare_date"],
            n_detections=int(r["n_detections"]),
            frp_sum_mw=r["frp_sum_mw"],
            median_frp_mw=r["median_frp_mw"],
            ratio=r["ratio"],
            z=r["z"],
            baseline_days=int(r["baseline_days"]),
            z_min=r["z_min"],
            threshold_method=r["threshold_method"],
            footprint_kind=r["footprint_kind"],
            radius_m=None if r["radius_m"] is None else int(r["radius_m"]),
            evidence=tuple(evidence_as_of(conn, r["refinery_id"], r["flare_date"], now)),
        )
        for r in rows
    ]


def _one_line(text: str) -> str:
    return _LINE_BREAKS.sub(" ", text).strip()


def _footprint_line(c: AlertCandidate) -> str:
    if c.footprint_kind == "osm_polygon":
        return "Footprint: OpenStreetMap outline."
    if c.footprint_kind == "osm_bounds":
        return "Footprint: approximate, the bounding box of an OpenStreetMap outline."
    return f"Footprint: approximate, a {c.radius_m} m circle round a point."


def _item_text(text: str | None) -> str:
    """One line, no control or bidi characters, at most ITEM_CHARS long."""
    spaced = _LINE_BREAKS.sub(" ", text or "")
    kept = "".join(ch for ch in spaced if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return kept.strip()[:ITEM_CHARS].strip()


def _item_line(item: Evidence) -> str:
    named = _item_text(item.unit_label)
    unit = named or ("whole plant" if item.whole_plant else "unit not stated")
    confidence = item.confidence or "n/a"
    return (
        f"{KIND_LABELS[item.kind]}: {_item_text(item.event_type) or 'event'} at {unit},"
        f" start {item.item_date:%Y-%m-%d}, confidence {confidence}."
    )


def _evidence_lines(c: AlertCandidate, now: datetime) -> list[str]:
    if not c.evidence:
        stamp = now.astimezone(DUBAI)
        return [f"News or regulator item: none found as of {stamp:%H:%M} Dubai time."]
    return ["News or regulator item: yes.", *(_item_line(i) for i in c.evidence[:MAX_ITEMS])]


def _normal_sentence(c: AlertCandidate) -> str:
    median = f"Its 90-day median is {c.median_frp_mw} MW over {c.baseline_days} scored days."
    if c.ratio is not None:
        usual = f"That is about {c.ratio} times normal."
    elif c.median_frp_mw == 0:
        usual = "It usually shows no detections."
    else:
        usual = "Its normal is under 1 MW a day."
    score = f"Score {c.z}, alert threshold {c.z_min} ({c.threshold_method})."
    return f"{median} {usual} {score}"


def _paragraphs(c: AlertCandidate, now: datetime) -> list[list[str]]:
    return [
        [_one_line(f"{c.name}, {c.country}, {c.region}.")],
        [
            f"Satellite detections on {c.flare_date:%Y-%m-%d} (UTC day): {c.n_detections},"
            f" total fire radiative power {c.frp_sum_mw} MW."
        ],
        [_normal_sentence(c)],
        [_footprint_line(c)],
        _evidence_lines(c, now),
        [FOOTER],
    ]


def render(c: AlertCandidate, now: datetime) -> tuple[str, str, str]:
    """Subject, plain text and HTML. No address and no key in any of them."""
    require_aware(now, "now")
    subject = _one_line(
        f"Flaring alert: {c.name}, {c.country}, {c.flare_date.day} {c.flare_date:%b}"
    )
    paragraphs = _paragraphs(c, now)
    text = "\n\n".join("\n".join(lines) for lines in paragraphs)
    style = "margin:0 0 12px 0"
    small = "margin:0;color:#555555;font-size:12px"
    blocks = []
    for index, lines in enumerate(paragraphs):
        inner = "<br>".join(html.escape(line) for line in lines)
        blocks.append(f'<p style="{small if index == len(paragraphs) - 1 else style}">{inner}</p>')
    page = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111111;'
        f'line-height:1.45">{"".join(blocks)}</div>'
    )
    return subject, text, page


def _figures(c: AlertCandidate) -> dict[str, str | int | None]:
    return {
        "n_detections": c.n_detections,
        "frp_sum_mw": str(c.frp_sum_mw),
        "median_frp_mw": str(c.median_frp_mw),
        "ratio": None if c.ratio is None else str(c.ratio),
        "z": str(c.z),
        "z_min": str(c.z_min),
        "threshold_method": c.threshold_method,
        "baseline_days": c.baseline_days,
        "footprint_kind": c.footprint_kind,
        "radius_m": c.radius_m,
    }


Claimed = int | Literal["capped", "already_claimed", "stale_day"]


def _claim(conn: Conn, c: AlertCandidate, email: Email, now: datetime, daily_max: int) -> Claimed:
    require_aware(now, "now")
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (CLAIM_LOCK,))
            today = conn.execute(
                "SELECT count(*) AS n FROM flare_alert"
                " WHERE alert_day = (clock_timestamp() AT TIME ZONE 'Asia/Dubai')::date"
            ).fetchone()
            if today is not None and int(today["n"]) >= daily_max:
                return "capped"
            row = conn.execute(
                "INSERT INTO flare_alert (refinery_id, flare_day_id, flare_date, figures,"
                " evidence_found, subject, body_text, body_html, send_fingerprint,"
                " idempotency_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    c.refinery_id,
                    c.flare_day_id,
                    c.flare_date,
                    Jsonb(_figures(c)),
                    bool(c.evidence),
                    email.subject,
                    email.text,
                    email.html,
                    fingerprint(email),
                    uuid.uuid4().hex,
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        return "already_claimed"
    except psycopg.Error as exc:
        if exc.sqlstate == "RW024":
            return "stale_day"
        raise
    if row is None:
        raise RuntimeError("insert returned no row")
    return int(row["id"])


def claim(conn: Conn, c: AlertCandidate, email: Email, now: datetime, daily_max: int) -> int | None:
    """Inserts the alert row under the day lock. None when capped or already claimed."""
    result = _claim(conn, c, email, now, daily_max)
    return result if isinstance(result, int) else None


def pending_retries(conn: Conn, now: datetime) -> list[int]:
    """Alerts with no sent attempt, fewer than 3 attempts and built within 18 hours."""
    require_aware(now, "now")
    rows = conn.execute(
        "SELECT a.id FROM flare_alert a"
        " WHERE a.built_at > %s - make_interval(secs => %s)"
        " AND NOT EXISTS (SELECT 1 FROM flare_alert_attempt t"
        "                 WHERE t.flare_alert_id = a.id AND t.status = 'sent')"
        " AND (SELECT count(*) FROM flare_alert_attempt t WHERE t.flare_alert_id = a.id) < %s"
        " ORDER BY a.id",
        (now, RETRY_WINDOW.total_seconds(), MAX_ATTEMPTS),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def deliver(conn: Conn, alert_id: int, mailer: Mailer, settings: AlertSettings) -> AlertOutcome:
    """Sends the stored payload with the stored key and records one attempt row."""
    alert = conn.execute(
        "SELECT subject, body_text, body_html, send_fingerprint, idempotency_key,"
        " clock_timestamp() - built_at > make_interval(secs => %s) AS too_old,"
        " (SELECT count(*) FROM flare_alert_attempt t WHERE t.flare_alert_id = a.id) AS attempts,"
        " EXISTS (SELECT 1 FROM flare_alert_attempt t"
        "         WHERE t.flare_alert_id = a.id AND t.status = 'sent') AS done"
        " FROM flare_alert a WHERE a.id = %s",
        (RETRY_WINDOW.total_seconds(), alert_id),
    ).fetchone()
    if alert is None:
        raise LookupError(f"flare_alert {alert_id} does not exist")
    if alert["done"]:
        return "sent"
    email = Email(
        settings.sender,
        settings.recipients,
        alert["subject"],
        alert["body_html"],
        alert["body_text"],
    )
    if fingerprint(email) != alert["send_fingerprint"]:
        raise RecipientsChangedError(f"flare_alert {alert_id} was built for another recipient list")
    if alert["too_old"] or int(alert["attempts"]) >= MAX_ATTEMPTS:
        return "send_failed"
    result = mailer.send(email, alert["idempotency_key"])
    with conn.transaction():
        conn.execute(
            "INSERT INTO flare_alert_attempt (flare_alert_id, attempt, status, http_status,"
            " provider_message_id, error) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                alert_id,
                int(alert["attempts"]) + 1,
                "sent" if result.ok else "send_failed",
                result.status,
                result.message_id if result.ok else None,
                None if result.ok else result.error,
            ),
        )
    return "sent" if result.ok else "send_failed"


def _refinery_of(conn: Conn, alert_id: int) -> str:
    row = conn.execute("SELECT refinery_id FROM flare_alert WHERE id = %s", (alert_id,)).fetchone()
    if row is None:
        raise LookupError(f"flare_alert {alert_id} does not exist")
    return str(row["refinery_id"])


def _deliver_safely(
    conn: Conn, alert_id: int, mailer: Mailer, settings: AlertSettings
) -> AlertOutcome:
    try:
        return deliver(conn, alert_id, mailer, settings)
    except RecipientsChangedError:
        log.warning("alert %d not sent: recipients changed since it was built", alert_id)
        return "send_failed"


def run_alerts(
    conn: Conn, mailer: Mailer | None, settings: AlertSettings, now: datetime
) -> list[tuple[str, AlertOutcome]]:
    """Retries first, then new candidates. With no mailer it renders and writes nothing."""
    require_aware(now, "now")
    out: list[tuple[str, AlertOutcome]] = []
    if mailer is None:
        return [(c.refinery_id, "dry_run") for c in candidates(conn, now)]
    for alert_id in pending_retries(conn, now):
        refinery = _refinery_of(conn, alert_id)
        out.append((refinery, _deliver_safely(conn, alert_id, mailer, settings)))
        conn.commit()
    conn.commit()
    for c in candidates(conn, now):
        subject, text, page = render(c, now)
        email = Email(settings.sender, settings.recipients, subject, page, text)
        claimed = _claim(conn, c, email, now, settings.daily_max)
        conn.commit()
        if isinstance(claimed, str):
            out.append((c.refinery_id, claimed))
            continue
        out.append((c.refinery_id, _deliver_safely(conn, claimed, mailer, settings)))
        conn.commit()
    return out


def render_all(conn: Conn, now: datetime) -> list[tuple[str, str, str, str]]:
    """(refinery_id, subject, text, html) for each candidate, for a dry run."""
    return [(c.refinery_id, *render(c, now)) for c in candidates(conn, now)]
