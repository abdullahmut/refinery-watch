# 013: Closed plants and sub-unit restarts

Date: 2026-11-11

Status: provisional. Uses the default in open decisions 3 and 4 in docs/definitions.md until the
owner answers them.

## Context

A mothballed plant coming back, such as part of Dalian in g005, is not a `restart` by the rules,
because it ends no outage. The note still needs to show it. Separately, g010 reports that two
small hydrotreating trains restarted with no capacity or date, which would make a restart row with
nothing a desk can use.

## Options

- Treat a reactivation as a `restart` row. It would end no open event, so it goes to review every
  time, and it would show as capacity returning.
- Treat it as a reference change. The loader writes the new `refinery.status` and an append-only
  `refinery_status_change` row, and the note shows a reference change line.
- For sub-unit restarts with no figure: write a capacity-null, low-confidence restart row, or
  write no row.

## Decision

A reactivation is a reference change. `refinery_status_change` records the old status, the new
status and when the loader saw it, and the note reads it as its own line, not as capacity
returning. A sub-unit restart with no figure produces no row. Validation records it as a non-event.

## Consequences

Capacity returning stays a sum of real restarts. Status changes arrive only through the operator's
edit to the local refinery file and the loader, so they lag the news by as long as the operator
takes. The schema already allows a capacity-null restart at low confidence, so changing decision 4
later is a validation change only. To undo decision 3, stop reading the status change table in the
note and route reactivations to review.

Half a page. Plain English.
