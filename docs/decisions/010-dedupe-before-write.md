# 010: Dedupe runs before the write, against events with no restart between

Date: 2026-11-11

Status: applies open decision 7 in docs/definitions.md and settles the "not settled" point in its
Dedupe section. Replaces the restart rule in record 004 and limits its outage rule. Provisional
until the owner confirms it.

## Context

The 3-day dedupe window ignored a restart in between. Restarts of two different outages merged,
and a re-trip after a restart merged back into the first outage. It was also open whether dedupe
runs on extractions or on rows already in `outage_event`. Rows there are never changed, so
"kept" and "linked" cannot mean editing or removing a row.

## Options

- Dedupe rows after they are written. Needs a merge marker on old rows, which is not allowed.
- Dedupe extractions before the write, by date window only. Simple, but still merges across a
  restart.
- Dedupe extractions before the write, against current rows, using the restart pointers. An
  outage is compared only with open outages. A restart is compared by the event it would end.

## Decision

The third. Two SQL functions do the lookup at write time.

- `outage_duplicate_of` returns outage chains on the same refinery, the same unit or one side
  with no named unit, and start dates within 3 days. A closed chain is a candidate only when its
  restart is dated after the report's start, so a late report of an outage that has already
  ended is the same event. A restart on or before the report's start separates the two, so
  nothing merges across a restart. A whole-plant row matches only a whole-plant row, so a
  plant stop and a damaged unit at the same site stay apart.
- `restart_duplicate_of` finds the outage the new restart would end, ignoring closure. If a
  restart already ends that chain, the new one is the same restart. There is no date window.

"Kept" means: if the new report has the higher confidence, it is written as a revision of the
current row, with its own event type. "Linked" means an `event_source` row with role
`corroboration`. Old rows are never touched. Amended 2026-11-11: the database write path never
writes the kept report itself; it returns the matching chain, and the extraction pipeline writes
the higher-confidence report as an ordinary revision of that chain's current row.

## Consequences

`write_event` runs the lookup for every new outage row. One match is returned as a duplicate and the
source is linked as corroboration. Several matches go to review. A re-trip after a restart is a new
outage, and two restarts of two outages stay two. With one open outage per unit (record 009) there
is at most one candidate per named unit. If an unnamed report matches several open units, it goes to
review. To undo, replace the two function bodies in a new migration.
