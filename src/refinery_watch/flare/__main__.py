"""Flare signal commands: python -m refinery_watch.flare footprints|fetch|backfill|alert|..."""

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Literal

import httpx
import psycopg

from refinery_watch.config import (
    ConfigError,
    ObjectStoreSettings,
    app_database_url,
    contact_email,
    fetch_ipv6,
    firms_map_key,
    flare_alert_daily_max,
    note_from,
    note_recipients,
    object_store_settings,
    refinery_locations_csv_path,
    resend_api_key,
)
from refinery_watch.db.connect import Conn, connect
from refinery_watch.flare import alert, calibrate, fetch, firms, footprints, overpass, research
from refinery_watch.mail.resend import Mailer, ResendClient
from refinery_watch.objects import ObjectStoreError, RawStore, S3RawStore
from refinery_watch.sources.clean import clean_text
from refinery_watch.sources.http import Fetcher, FetchError, Resolver, utc_now
from refinery_watch.sources.store import (
    RUN_BUDGET,
    RunNotRunning,
    RunOutcome,
    SourceMissing,
    SourceNotCleared,
    finish_run,
    job_name,
    source_row,
    start_run,
)

QUIET_LOGGERS: Final = ("httpx", "httpcore", "urllib3")
STATEMENT_TIMEOUT: Final = "30s"
LONG_STATEMENT_TIMEOUT: Final = "120s"
OK, FAILED, USAGE, REFUSED = 0, 1, 2, 3
RESULTS_FILE_GUARD: Final = "flare-lead-time.md"
ALERT_INTERVAL_MIN: Final = 30
CALIBRATE_INTERVAL_MIN: Final = 1
MAX_FOOTPRINT_REQUESTS: Final = 100
MAX_BACKFILL_REQUESTS: Final = 1000
log = logging.getLogger("refinery_watch.flare")

Command = Literal["footprints", "fetch", "backfill", "alert", "calibrate", "research"]


def _setup_logging() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for old in list(log.handlers):
        log.removeHandler(old)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD") from None


def _instant(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an ISO date and time") from None
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("the time needs a zone, for example +00:00")
    return value


def _count(limit: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("expected a whole number") from None
        if not 1 <= value <= limit:
            raise argparse.ArgumentTypeError(f"expected a number from 1 to {limit}")
        return value

    return parse


def _target(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("expected a number") from None
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError("expected a number of zero or more")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m refinery_watch.flare")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("footprints")
    one.add_argument("--max-requests", type=_count(MAX_FOOTPRINT_REQUESTS), default=100)
    sub.add_parser("fetch")
    back = sub.add_parser("backfill")
    back.add_argument("--from", dest="first", type=_day, required=True)
    back.add_argument("--to", dest="last", type=_day, required=True)
    back.add_argument("--max-requests", type=_count(MAX_BACKFILL_REQUESTS), default=150)
    send = sub.add_parser("alert")
    send.add_argument("--send", action="store_true")
    cal = sub.add_parser("calibrate")
    cal.add_argument("--kind", choices=("signal", "alert"), required=True)
    cal.add_argument("--target", type=_target)
    cal.add_argument("--write", action="store_true")
    res = sub.add_parser("research")
    res.add_argument("--as-of", dest="as_of", type=_instant)
    res.add_argument("--out", type=Path, required=True)
    return parser


def _report(job: str, outcome: RunOutcome) -> None:
    log.info(
        "job=%s status=%s rows_in=%d rows_out=%d note=%s",
        job,
        outcome.status,
        outcome.rows_in,
        outcome.rows_out,
        clean_text(outcome.note) if outcome.note else "-",
    )


@dataclass(frozen=True, slots=True)
class Injected:
    transport: httpx.BaseTransport | None
    resolve: Resolver | None
    store: RawStore | None
    mailer: Mailer | None
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything a command needs from the environment, read before the database is opened."""

    email: str = ""
    store: ObjectStoreSettings | None = None
    firms_key: str = ""
    locations: Sequence[footprints.Location] = ()
    alerts: alert.AlertSettings | None = None
    resend_key: str | None = None


def _load_settings(args: argparse.Namespace, inj: Injected) -> Settings:
    command: Command = args.command
    if command in ("footprints", "fetch", "backfill"):
        return Settings(
            email=contact_email(),
            store=object_store_settings(),
            firms_key=firms_map_key() if command != "footprints" else "",
            locations=(
                footprints.read_locations(refinery_locations_csv_path())
                if command == "footprints"
                else ()
            ),
        )
    if command == "alert" and args.send:
        return Settings(
            alerts=alert.AlertSettings(note_from(), note_recipients(), flare_alert_daily_max()),
            resend_key=resend_api_key() if inj.mailer is None else None,
        )
    return Settings()


def _set_timeout(conn: Conn, value: str) -> None:
    conn.execute(f"SET statement_timeout = '{value}'")
    conn.commit()


def _guarded(job: str, body: Callable[[], RunOutcome]) -> RunOutcome:
    """Runs a job body; an unexpected error is logged by type only, never by text."""
    try:
        return body()
    except (FetchError, ObjectStoreError) as exc:
        return RunOutcome("failed", 0, 0, type(exc).__name__)
    except Exception as exc:
        log.error("unexpected error in %s: %s", job, type(exc).__name__)
        return RunOutcome("failed", 0, 0, type(exc).__name__)


def _run_job(conn: Conn, job: str, interval_min: int, body: Callable[[int], RunOutcome]) -> int:
    run_id = start_run(conn, job, interval_min)
    if run_id is None:
        _report(job, RunOutcome("skipped", 0, 0, "see job_run"))
        return OK
    outcome = _guarded(job, lambda: body(run_id))
    try:
        finish_run(conn, run_id, outcome)
    except RunNotRunning as exc:
        log.error("job=%s status=failed note=%s", job, clean_text(str(exc)))
        return FAILED
    _report(job, outcome)
    return FAILED if outcome.status == "failed" else OK


def _fetch_command(
    command: str, args: argparse.Namespace, conn: Conn, inj: Injected, cfg: Settings
) -> int:
    source_id = overpass.SOURCE_ID if command == "footprints" else firms.SOURCE_ID
    try:
        row = source_row(conn, source_id)
    except (SourceMissing, SourceNotCleared) as exc:
        log.error("job=%s status=failed note=%s", job_name(source_id), clean_text(str(exc)))
        return FAILED
    if inj.store is not None:
        raw_store: RawStore = inj.store
    elif cfg.store is not None:
        raw_store = S3RawStore(cfg.store)
    else:
        raise ConfigError("the object store is not configured")

    def body(run_id: int) -> RunOutcome:
        with Fetcher(
            cfg.email, transport=inj.transport, resolve=inj.resolve, ipv6=fetch_ipv6()
        ) as fetcher:
            deadline = fetcher.monotonic() + RUN_BUDGET.total_seconds()
            if command == "footprints":
                return footprints.run_footprints(
                    conn, fetcher, raw_store, run_id, cfg.locations, args.max_requests, deadline
                )
            if command == "fetch":
                today = inj.now().astimezone(UTC).date()
                return fetch.run_fetch(
                    conn, fetcher, raw_store, run_id, cfg.firms_key, today, deadline
                )
            return fetch.run_backfill(
                conn,
                fetcher,
                raw_store,
                run_id,
                cfg.firms_key,
                args.first,
                args.last,
                args.max_requests,
                deadline,
            )

    job = "flare_backfill" if command == "backfill" else job_name(source_id)
    return _run_job(conn, job, row.fetch_interval_min, body)


def _alert_dry_run(conn: Conn, inj: Injected) -> int:
    lines = alert.render_all(conn, inj.now())
    conn.rollback()
    if not lines:
        sys.stdout.write("No alert candidates.\n")
        return OK
    for refinery_id, subject, text, _page in lines:
        sys.stdout.write(f"=== {refinery_id} ===\nSubject: {subject}\n\n{text}\n\n")
    return OK


def _alert_outcome(results: Sequence[tuple[str, alert.AlertOutcome]]) -> RunOutcome:
    for refinery_id, outcome in results:
        log.info("alert refinery=%s outcome=%s", refinery_id, outcome)
    failures = [r for r, o in results if o == "send_failed"]
    note = f"send failed for {', '.join(failures)}" if failures else None
    status: Literal["ok", "failed"] = "failed" if failures else "ok"
    return RunOutcome(status, len(results), len(results) - len(failures), note)


def _alert_send(conn: Conn, inj: Injected, cfg: Settings) -> int:
    settings = cfg.alerts
    if settings is None:
        raise ConfigError("alert settings are not loaded")

    def body(run_id: int) -> RunOutcome:
        if inj.mailer is not None:
            return _alert_outcome(alert.run_alerts(conn, inj.mailer, settings, inj.now()))
        with ResendClient(cfg.resend_key or "") as client:
            return _alert_outcome(alert.run_alerts(conn, client, settings, inj.now()))

    return _run_job(conn, "flare_alert", ALERT_INTERVAL_MIN, body)


def _alert_command(args: argparse.Namespace, conn: Conn, inj: Injected, cfg: Settings) -> int:
    return _alert_send(conn, inj, cfg) if args.send else _alert_dry_run(conn, inj)


def _print_curve(points: Sequence[calibrate.CurvePoint]) -> None:
    sys.stdout.write("z_min  flagged_quiet_days  quiet_plant_days  false_alarms_per_plant_month\n")
    for p in points:
        sys.stdout.write(
            f"{p.z_min:>5}  {p.flagged_quiet_days:>18}  {p.quiet_plant_days:>16}"
            f"  {p.false_alarms_per_plant_month}\n"
        )


def _calibrate_body(args: argparse.Namespace, conn: Conn, inj: Injected) -> RunOutcome:
    as_of = inj.now()
    span = conn.execute(
        "SELECT min(day) AS first_day, max(day) AS last_day FROM flare_day_current_as_of(%s)",
        (as_of,),
    ).fetchone()
    if span is None or span["first_day"] is None:
        raise calibrate.NotEnoughHistory("no scored flare days are loaded")
    points = calibrate.curve(conn, args.kind, span["first_day"], span["last_day"], as_of)
    target = args.target if args.target is not None else calibrate.DEFAULT_TARGET[args.kind]
    _print_curve(points)
    chosen = calibrate.choose(points, target)
    if chosen is None:
        sys.stdout.write(f"No grid value meets the target of {target} per plant-month.\n")
        return RunOutcome("failed" if args.write else "ok", len(points), 0, "no value meets target")
    sys.stdout.write(f"Chosen z_min {chosen.z_min} for a target of {target} per plant-month.\n")
    if not args.write:
        return RunOutcome("ok", len(points), 0, "curve only; nothing written")
    row = conn.execute(
        "SELECT min_detections FROM flare_threshold_as_of(%s, %s) WHERE id IS NOT NULL",
        (args.kind, as_of),
    ).fetchone()
    min_detections = 1 if row is None else int(row["min_detections"])
    params = {
        "target": str(target),
        "first_day": str(span["first_day"]),
        "last_day": str(span["last_day"]),
        "quiet_plant_days": str(chosen.quiet_plant_days),
    }
    calibrate.write_threshold(conn, args.kind, chosen, min_detections, params)
    conn.commit()
    return RunOutcome("ok", len(points), 1, None)


def _calibrate_command(args: argparse.Namespace, conn: Conn, inj: Injected) -> int:
    _set_timeout(conn, LONG_STATEMENT_TIMEOUT)
    refused: list[str] = []

    def body(run_id: int) -> RunOutcome:
        try:
            return _calibrate_body(args, conn, inj)
        except calibrate.NotEnoughHistory as exc:
            refused.append(str(exc))
            return RunOutcome("failed", 0, 0, str(exc))

    code = _run_job(conn, "flare_calibrate", CALIBRATE_INTERVAL_MIN, body)
    return REFUSED if refused else code


def _research_command(args: argparse.Namespace, conn: Conn, inj: Injected) -> int:
    if args.out.name == RESULTS_FILE_GUARD:
        sys.stderr.write(f"research writes the results file only, not {RESULTS_FILE_GUARD}\n")
        return USAGE
    _set_timeout(conn, LONG_STATEMENT_TIMEOUT)
    as_of = args.as_of or inj.now()
    signal = research.figures(conn, as_of, "signal")
    alerts = research.figures(conn, as_of, "alert")
    conn.rollback()
    text = research.render_results(signal, alerts)
    args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
    log.info("research written scored_plant_days=%d", signal.scored_plant_days)
    return OK


def _dispatch(args: argparse.Namespace, conn: Conn, inj: Injected, cfg: Settings) -> int:
    command: Command = args.command
    if command in ("footprints", "fetch", "backfill"):
        return _fetch_command(command, args, conn, inj, cfg)
    if command == "alert":
        return _alert_command(args, conn, inj, cfg)
    if command == "calibrate":
        return _calibrate_command(args, conn, inj)
    return _research_command(args, conn, inj)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    resolve: Resolver | None = None,
    store: RawStore | None = None,
    mailer: Mailer | None = None,
    now: Callable[[], datetime] = utc_now,
) -> int:
    _setup_logging()
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else USAGE
    inj = Injected(transport, resolve, store, mailer, now)
    try:
        url = app_database_url()
        cfg = _load_settings(args, inj)
    except (ConfigError, footprints.LocationsError) as exc:
        sys.stderr.write(f"{exc}\n")
        return USAGE
    try:
        conn = connect(url)
    except psycopg.OperationalError as exc:
        sys.stderr.write(f"{exc}\n")
        return FAILED
    try:
        if args.command != "calibrate":
            _set_timeout(conn, STATEMENT_TIMEOUT)
        return _dispatch(args, conn, inj, cfg)
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return USAGE
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
