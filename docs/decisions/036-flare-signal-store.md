# 036: A separate append-only store for the flare signal

Date: 2026-11-12

## Context

Satellite fire detections at refineries are a new kind of data. They are not events, they carry
no capacity, and they arrive as partial days that fill in over the next day. They must still
answer an as-of question: what did the system know at time T. `outage_event` is append-only and
holds only events backed by a news, company or regulator source.

## Options

- Write flare days into `outage_event` as a new event type. That puts a signal next to capacity,
  breaks the primary-source rule, and lets a sum over current rows count a flare as an outage.
- Mark outage rows with a flare flag or a raised confidence. That is an update, which is revoked,
  and it would leak later knowledge into as-of queries.
- Separate tables, append-only in the same way: revisions are new rows with `supersedes`, current
  rows are those no later row supersedes, UPDATE, DELETE and TRUNCATE are revoked and refused by
  trigger. Links to outages are made at query time, in SQL functions that take `as_of`.

## Decision

Separate tables in migration r0009: `plant_footprint`, `flare_detection`, `flare_coverage`,
`flare_day`, `flare_threshold`, `flare_alert` and `flare_alert_attempt`. All are SELECT and INSERT
only for the app role. `flare_day` keeps a `known_at` from the database clock and an
`available_at`, which is the same for live rows and reconstructed for backfilled rows. Nothing in
this feature writes to `outage_event` or `event_source`.

## Consequences

The outage tables and their triggers are untouched. Every flare figure has a known time, so the
note and the research can ask the same as-of question of both stores. A wrong flare day is fixed
by a new row, never by an edit. feat/note also planned r0009; whichever branch merges second
renumbers its migration. To undo, stop the jobs; the tables can stay, since nothing else reads them.
