# 011: Whole-plant restarts end only whole-plant outages

Date: 2026-11-11

Status: proposed answer to open decision 5 in docs/definitions.md. Provisional until the owner
confirms it.

## Context

A source says "the refinery is back online" while named units there are still open. A `restart`
row holds one `ends_event_id`, so it can end one event. The reverse also comes up: a unit restart
while a whole-plant outage is open.

## Options

- Keep the rule as written. A whole-plant restart ends only an open whole-plant outage, and goes to
  review if there is none. A unit restart never ends a whole-plant outage. No schema cost.
- Write one `restart` row per open unit at that refinery. No schema cost, but it closes units the
  source did not mention.
- Let one restart close every open event at the refinery. Needs a separate link table in place of
  the single pointer, and changes `outage_as_of`.

## Decision

Keep the rule as written. Golden item g008 shows why: Valero Port Arthur had a whole-plant stop and
a damaged diesel hydrotreater open at once. The plant came back before the hydrotreater. A
whole-plant restart that closed every open event would have shown the hydrotreater running. When a
whole-plant restart finds no open whole-plant outage but finds open units, it goes to review with
reason `plant_restart_units_open`. The operator writes one restart per unit the source supports.

## Consequences

No unit is closed on a source that did not name it. Some whole-plant restarts wait in review. A
round-up that lists the units coming back still produces one restart per unit through extraction.
To move to "close everything", add an append-only link table from a restart to the events it ends
and read it in `outage_as_of`. Rows already written stay valid.
