# 004: Dedupe by event family

Date: 2026-11-11

Status: provisional until the `outage_event` schema spec. Open decision 7 in docs/definitions.md
(no merge across a restart; restarts dedupe by the event they end) replaces the restart rule here
and limits the outage rule.

## Context

The Dedupe section of docs/definitions.md first treated any two extractions for the same refinery,
unit and start window as one event. That merged a shutdown with its own restart. Requiring the
same event type fixed that, but then one outage that one source calls planned and another calls
unplanned became two events. Its capacity was counted twice in the note.

## Options

- Same event type only. Never merges a shutdown with its restart. Counts capacity twice when
  sources disagree on planned or unplanned, which is common early in an outage.
- Any event type. Counts each outage once, but merges a shutdown with its restart, so the
  restart is lost.
- Two families: outages (`planned` and `unplanned`) and restarts (`restart`). Dedupe compares
  only within a family. The cost is that two separate outages of different types on the same unit
  within 3 days would be merged.

## Decision

Two families. An outage is compared only with outages, a restart only with restarts. When an
outage is merged, the row with the higher confidence is kept with its own event type, and the
others are linked as corroboration.

## Consequences

Each outage counts once in capacity offline, and a restart never swallows the event it ends.
Disagreeing sources on planned or unplanned now add corroboration. The kept type follows
confidence, so a low-confidence label never overrides a company statement. A real trip followed
within 3 days by an unrelated turnaround on the same unit would merge; we accept that as rare.
There is no tie-break yet when confidence is equal, and whether dedupe runs before rows reach
`outage_event` is still open. To undo, compare event type again and accept the double count.
