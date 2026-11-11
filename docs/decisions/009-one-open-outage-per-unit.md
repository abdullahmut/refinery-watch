# 009: One open outage per unit, held by an insert trigger

Date: 2026-11-11

Status: applies open decision 6 in docs/definitions.md. Provisional until the owner confirms it.

## Context

A unit must have at most one open outage, or its capacity counts offline twice. Whether an event
is open depends on a later `restart` row pointing at its chain through `ends_event_id`. No row is
ever updated, so nothing on the outage row itself can say "closed". A plain unique index cannot
see a pointer on another row.

## Options

- Exclusion constraint on a date range. The range would have to be closed by changing the row,
  which is not allowed.
- Stored open-chain key: each new outage stores the id of the restart that freed the unit, under
  a unique index. Safe under concurrency with no lock. It breaks when a revision moves an open
  event to a named unit, which is a common revision, and it still needs a trigger to check the
  pointer.
- Locked insert in application code. Holds only while every writer uses the same code path.
- A `BEFORE INSERT` trigger on `outage_event` that takes one transaction advisory lock for all
  writes to the table, then checks the new row against `outage_as_of`. The cost is that writes to
  the table run one at a time.

## Decision

The trigger. It is in the database, so no writer can skip it, and it sees the same open state
that `outage_as_of` returns. Volume is 5 to 15 rows a day, so one lock for the table costs
nothing. The trigger refuses to run outside READ COMMITTED, because only then does each query
after the lock see rows committed while it waited. It also sets `known_at` under the lock, so
write order and `known_at` order are the same.

A new shutdown on a unit that already has an open outage is refused. The write path sends it to
review. It does not supersede the open event, because that would fold two outages into one and
hide the first start date. A report of the same outage is caught earlier by dedupe (record 010).

## Consequences

The unit never counts twice, and a refined products desk sees one figure per unit. A missed
restart leaves the unit blocked, and every later outage on it waits in review until the operator
writes the restart. A superuser can still disable the trigger; the app role cannot. To undo, drop
the open-outage check from the trigger function in a new migration.

Half a page. Plain English.
