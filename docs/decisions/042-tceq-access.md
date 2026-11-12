# 042: How we read TCEQ air emission event reports

Date: 2026-11-12

## Context

Texas plants must report to TCEQ any emission event above set thresholds, and those reports are
public. An earlier check recorded `robots_refused_fetcher_must_verify` for `tceq_eer` in
data/sources.csv. Our fetcher has never contacted TCEQ, because the gate has never let
`tceq_eer` through.

## Options

- The public EER web pages (the `feed_url` in sources.csv), if robots.txt and the site terms
  allow them. No new format, but the earlier check suggests a refusal.
- A Texas open data portal export. sources.csv names data.texas.gov. Whether an emission event
  dataset exists there, its name, and how often it updates are all to confirm. Structured and
  official, but it may lag the web pages.
- A Public Information Request. Lawful, but its response time is to confirm, so it suits a
  one-off backfill, not a daily note.
- Skip TCEQ. No risk, but we lose a regulator signal for the US Gulf. Whether it comes before
  the news is to confirm, since nothing has been measured.

## Decision

To a refined products desk, an emission event is a regulator signal, not capacity. It says a
plant reported a release above a threshold. It does not say a unit stopped or cut rates. The
rules in definitions.md for regulator records stand as written.

No fetch of any TCEQ or data.texas.gov host until the operator reads robots.txt and the site
terms in a browser. If an official open-data export of emission events exists, we use it ahead
of the web pages. The web pages are used only if robots.txt allows our paths and the terms allow
automated reading. If neither route is open, TCEQ stays skipped.

To clear a route, the operator first records the date and the result in this record. Only then
does `robots_status` for `tceq_eer` become `open` in data/sources.csv. If the export is used,
`feed_url` and `access_method` change to match it. Then update `SOURCES_CSV_SHA256` in
tests/test_sources_csv.py, as record 035 does, and reload the source list.

LDEQ incident reports follow the same rule in a later branch.

## Consequences

`tceq_event` stays empty until a route is cleared. While no route is cleared, no row is lifted
by a regulator record. `QUIET_SQL` in flare/calibrate.py uses `tceq_event` to remove days near a
regulator record from the quiet baseline. While the table is empty, those days count as quiet,
so calibration runs on a noisier baseline. The `regulator_near` context in flare evidence is
always empty. An export may need its own adapter. To undo, set the status back to a refused
value, update the hash and reload.
