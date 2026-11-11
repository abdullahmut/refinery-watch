"""Runs one source: python -m refinery_watch.sources hp_news|hp_refining|gdelt_doc|eia_wpsr."""

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Final, Literal, get_args

import httpx

from refinery_watch.config import (
    ConfigError,
    contact_email,
    database_url,
    eia_api_key,
    object_store_settings,
)
from refinery_watch.db.connect import Conn, connect
from refinery_watch.extract.prefilter import build_term_matcher
from refinery_watch.extract.reference import load_refineries
from refinery_watch.objects import ObjectStoreError, RawStore, S3RawStore
from refinery_watch.sources import eia_wpsr, gdelt_doc, hp_rss
from refinery_watch.sources.http import Fetcher, FetchError, Resolver, resolve_host
from refinery_watch.sources.store import (
    RUN_BUDGET,
    RunOutcome,
    SourceMissing,
    SourceNotCleared,
    SourceRow,
    finish_run,
    job_name,
    source_row,
    start_run,
)

SourceName = Literal["hp_news", "hp_refining", "gdelt_doc", "eia_wpsr"]
QUIET_LOGGERS: Final = ("httpx", "httpcore", "urllib3")
STATEMENT_TIMEOUT: Final = "30s"
log = logging.getLogger("refinery_watch.sources")


def _setup_logging() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for old in list(log.handlers):
        log.removeHandler(old)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _report(job: str, outcome: RunOutcome) -> None:
    log.info(
        "job=%s status=%s rows_in=%d rows_out=%d note=%s",
        job,
        outcome.status,
        outcome.rows_in,
        outcome.rows_out,
        outcome.note or "-",
    )


def _dispatch(
    name: SourceName,
    conn: Conn,
    fetcher: Fetcher,
    store: RawStore,
    run_id: int,
    row: SourceRow,
    eia_key: str | None,
) -> RunOutcome:
    deadline = fetcher.monotonic() + RUN_BUDGET.total_seconds()
    if name == "eia_wpsr":
        if eia_key is None:
            raise ConfigError("EIA_API_KEY is not set")
        return eia_wpsr.run(conn, fetcher, store, run_id, row.feed_url, eia_key)
    matcher = build_term_matcher(load_refineries(conn))
    conn.rollback()
    if name == "gdelt_doc":
        return gdelt_doc.run(
            conn, fetcher, store, matcher, run_id, row.feed_url, row.fetch_interval_min, deadline
        )
    return hp_rss.run(conn, fetcher, store, matcher, name, run_id, row.feed_url, deadline)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    resolve: Resolver = resolve_host,
    store: RawStore | None = None,
) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="python -m refinery_watch.sources")
    parser.add_argument("source", choices=get_args(SourceName))
    try:
        name: SourceName = parser.parse_args(argv).source
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        url = database_url()
        email = contact_email()
        settings = object_store_settings()
        eia_key = eia_api_key() if name == "eia_wpsr" else None
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    job = job_name(name)
    conn = connect(url)
    try:
        conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.commit()
        try:
            row = source_row(conn, name)
        except (SourceMissing, SourceNotCleared) as exc:
            log.error("job=%s status=failed note=%s", job, exc)
            return 1
        run_id = start_run(conn, job, row.fetch_interval_min)
        if run_id is None:
            _report(job, RunOutcome("skipped", 0, 0, "see job_run"))
            return 0
        raw_store = store or S3RawStore(settings)
        outcome = _run_guarded(
            name, conn, raw_store, run_id, row, email, eia_key, transport, resolve
        )
        finish_run(conn, run_id, outcome)
        _report(job, outcome)
        return 1 if outcome.status == "failed" else 0
    finally:
        conn.close()


def _run_guarded(
    name: SourceName,
    conn: Conn,
    store: RawStore,
    run_id: int,
    row: SourceRow,
    email: str,
    eia_key: str | None,
    transport: httpx.BaseTransport | None,
    resolve: Resolver,
) -> RunOutcome:
    try:
        with Fetcher(email, transport=transport, resolve=resolve) as fetcher:
            return _dispatch(name, conn, fetcher, store, run_id, row, eia_key)
    except (FetchError, ObjectStoreError) as exc:
        return RunOutcome("failed", 0, 0, f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        log.exception("unexpected error in %s", job_name(name))
        return RunOutcome("failed", 0, 0, type(exc).__name__)


if __name__ == "__main__":
    sys.exit(main())
