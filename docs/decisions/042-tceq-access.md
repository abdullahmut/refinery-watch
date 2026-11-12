# 042: How we read TCEQ air emission event reports

Date: 2026-11-12

## Context

Texas refineries must report air emission events to TCEQ. The reports are public and often
come out before the news, so they are worth reading. The `tceq_eer` row in data/sources.csv has
robots status `robots_refused_fetcher_must_verify`: our fetcher was refused, and no person has
read the robots.txt or the site terms yet. The gate refuses that status, so nothing runs now.
Record 015 says we never evade a block.

To a refined products desk, an emission event is a regulator signal, not capacity. It says a
plant flared or released something. It does not say a unit stopped or cut rates. Per
definitions.md, an event with no stated unit or throughput effect stays in `tceq_event` as a
signal. A regulator record on its own is `low`, and the note shows it as a signal, never as
capacity offline, until a news or company source confirms a throughput effect.

## Options

- The public EER web pages (the `feed_url` in sources.csv), if robots.txt and the terms allow
  them. Cheap, but the fetcher has already been refused once.
- A Texas open data portal export. sources.csv names data.texas.gov. Whether an emission event
  dataset exists there, what it is called, and how often it updates are all to confirm.
  Structured and official, but it may lag the web pages.
- A Public Information Request. Lawful and certain, but slow, and no use for a daily note. Good
  for a one-off backfill only.
- Skip TCEQ. No risk, but we lose the earliest public signal for the US Gulf.

## Decision

No fetch of any TCEQ host until the operator reads robots.txt and the site terms in a browser
and writes the date and the result here. If an official open-data export of emission events
exists, we use it ahead of the web pages, even if it is slower. The web pages are used only if
robots.txt allows our paths and the terms allow automated reading. If neither route is open, we
skip TCEQ and keep the row refused. A Public Information Request is for backfill only. When the
operator clears a route, they set the matching status in data/sources.csv, update its hash in
the source list test, and reload the sources.

LDEQ incident reports and the Bay Area air district flaring notices follow the same rule in a
later branch: no fetch until robots.txt and the terms are read, and an official export comes
first.

## Consequences

Until a route is cleared, `tceq_event` gets no rows, so no outage is ever lifted by a regulator
record. The flare work that joins `tceq_event` still runs and finds nothing there. An open-data
export may need a new adapter rather than the HTML one. To undo, set the status back to a
refused value and reload the sources.
