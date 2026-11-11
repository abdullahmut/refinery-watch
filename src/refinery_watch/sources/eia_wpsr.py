"""EIA weekly refinery utilisation through API v2. Values go to the price table."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from refinery_watch.db.connect import Conn
from refinery_watch.objects import RawStore
from refinery_watch.sources.http import Fetcher, FetchError
from refinery_watch.sources.store import (
    NewDocument,
    PriceRow,
    RunOutcome,
    Stored,
    store_document,
    write_prices,
)

SOURCE_ID: Final = "eia_wpsr"
SERIES: Final[Mapping[str, str]] = {
    "TESTSERIES1": "eia.testseries1",
    "TESTSERIES2": "eia.testseries2",
}
LENGTH: Final = 200
HEADLINE: Final = "EIA weekly refinery inputs and utilisation"
REDACTED: Final = b"REDACTED"


class EiaResponseError(ValueError):
    """EIA returned an error or a body without data rows."""


def build_params(api_key: str) -> list[tuple[str, str]]:
    params = [("api_key", api_key), ("frequency", "weekly"), ("data[0]", "value")]
    params.extend(("facets[series][]", series) for series in SERIES)
    params.extend(
        [("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", str(LENGTH))]
    )
    return params


def _value(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise EiaResponseError("EIA row has a value that is not a number") from None


def parse_data(body: bytes) -> list[PriceRow]:
    try:
        data = json.loads(body.decode("utf-8", errors="replace"), strict=False)
    except ValueError:
        raise EiaResponseError("EIA reply is not JSON") from None
    if not isinstance(data, dict):
        raise EiaResponseError("EIA reply is not a JSON object")
    if "error" in data:
        code = data.get("code")
        raise EiaResponseError(f"EIA returned an error (code {code})")
    response = data.get("response")
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise EiaResponseError("EIA reply has no response.data")
    out: list[PriceRow] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("series") not in SERIES:
            continue
        value = _value(row.get("value"))
        if value is None:
            continue
        try:
            observed = date.fromisoformat(str(row.get("period")))
        except ValueError:
            raise EiaResponseError("EIA row has a bad period") from None
        out.append(PriceRow(SERIES[row["series"]], observed, value, str(row.get("units") or "")))
    return out


def normalised_content(rows: Sequence[PriceRow]) -> str:
    ordered = sorted(rows, key=lambda r: (r.series, r.observed_on, r.unit, r.value))
    return json.dumps(
        [
            {
                "series": r.series,
                "observed_on": r.observed_on.isoformat(),
                "value": format(r.value.normalize(), "f"),
                "unit": r.unit,
            }
            for r in ordered
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def redact(body: bytes, api_key: str) -> bytes:
    if not api_key:
        return body
    return body.replace(api_key.encode(), REDACTED)


def run(
    conn: Conn, fetcher: Fetcher, store: RawStore, run_id: int, feed_url: str, api_key: str
) -> RunOutcome:
    try:
        reply = fetcher.get(feed_url, params=build_params(api_key), accept="application/json")
    except FetchError as exc:
        return RunOutcome("failed", 0, 0, f"{type(exc).__name__}: {exc}")
    body = redact(reply.body, api_key)
    if api_key.encode() in body:
        raise RuntimeError("key redaction failed")
    try:
        rows = parse_data(body)
    except EiaResponseError as exc:
        return RunOutcome("failed", 0, 0, str(exc))
    if not rows:
        return RunOutcome("failed", 0, 0, "no allowlisted series")
    content = normalised_content(rows)
    result = store_document(
        conn,
        store,
        run_id,
        NewDocument(
            source_id=SOURCE_ID,
            url=feed_url,
            original=body,
            ext="json",
            content_type="application/json",
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            headline=HEADLINE,
            body_chars=len(content),
            fetched_at=reply.fetched_at,
            published=None,
            prefilter_terms=(),
        ),
    )
    if not isinstance(result, Stored):
        return RunOutcome("ok", len(rows), 0, "unchanged")
    written = write_prices(conn, rows, SOURCE_ID, result.raw_document_id)
    return RunOutcome("ok", len(rows), written, None)
