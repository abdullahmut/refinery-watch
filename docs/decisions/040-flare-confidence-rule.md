# 040: When flaring raises an event's confidence

Date: 2026-11-12

## Context

Flaring often goes with an upset, a trip or a shutdown, but plants also flare during normal work,
and a flare says nothing about which unit or how much capacity. definitions.md computes confidence
from source kind, stated capacity, match score and corroboration. Rows in `outage_event` are never
changed, so a raised confidence cannot be written onto the row.

## Options

- Count a flare as one more independent source. That lifts `medium` to `high` on a satellite
  pixel, which is too much.
- Show flaring next to the confidence but never change it. Safe, but it ignores the evidence.
- Lift `low` to `medium` only, at query time, only for rows that already meet every `medium`
  condition except the source one, and only with a flagged day close to the event's start.

## Decision

The third. As of T, an outage row's effective confidence is `medium` when all of these hold:
stored confidence is `low`; event type is `unplanned` or `planned`; `match_score >= 90`; a unit is
named or the event is whole-plant; `capacity_basis <> 'not_stated'`; `start_unknown` and `old_start`
are false; the chain has at least one source known by T whose kind is not `regulator`; and a current
`flare_day` known by T for that refinery meets the signal threshold in force at T, with its day from
`start_date - 3` to `start_date + 3`. Otherwise it is the stored value. `medium` and `high` never
change. The window matches the 3-day dedupe window. The SQL function `outage_confidence_as_of(T)`
computes this. Nothing is stored on the outage row.

## Consequences

Flaring never creates an event, never adds capacity and never makes a regulator-only row look like
a confirmed outage, as definitions.md requires. A late flare row cannot change an earlier as-of
answer, because both sides are filtered by known time. The lift is small on purpose. To undo, stop
calling the function; the stored confidence was never changed.
