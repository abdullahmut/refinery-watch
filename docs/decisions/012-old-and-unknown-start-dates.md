# 012: Old and unknown start dates

Date: 2026-11-11

Status: provisional. Uses the "Leaning" of open decisions 1 and 2 in docs/definitions.md until the
owner answers them.

## Context

Two golden items break the date rules. Al Zour's March outage is described in a July article,
outside the 90-day window (g004). Marifu's CDU "remained shut" with no start date at all (g006).
Rejecting both loses true outages. Accepting them needs flags, and decision 2 also asks whether a
placeholder start takes part in dedupe and restart matching.

## Options

- Reject both. No schema cost; the desk never sees them.
- Widen the window to 180 days. Catches some, still loses older ones, no flag to warn the reader.
- Accept with flags. An old start is written with `confidence = low` and `old_start = true`. A
  missing start is written with `start_date` set to the publication date and
  `start_unknown = true`. Two boolean columns.

## Decision

Accept with flags. A desk would rather see a flagged row than none. A placeholder start takes part
in restart matching, so a later restart can close the event. In dedupe, the 3-day window is
waived when either side has `start_unknown`, so a later report with the real start becomes a
revision of the open event, not a second outage. `known_at` is unaffected either way. Check
constraints hold the flags to their confidence: `old_start` only at `low`, `start_unknown` never
at `high`. Restart rows carry neither flag.

## Consequences

The note can say "start not stated" or "start date old, low confidence" next to a row. A
placeholder start can make a later restart look earlier than the outage it ends; matching uses the
placeholder, so such a restart goes to review. To undo, stop writing the flags in validation. The
columns can stay at false, or go in a later migration.
