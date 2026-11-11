# Definitions

These rules decide what goes into `outage_event`, how it is classified, and how confident the
system says it is. The extraction prompt, the validation rules and the note all follow this
file. Change it here first.

## What counts as an event

An event is a change in the operating state of a named refinery unit, or a whole plant: a stop, a
rate cut of 10 percent or more, or a return to service, where the stop or cut lasts or is expected
to last at least 24 hours. Three types:

| event_type | Meaning | Typical wording in sources |
| --- | --- | --- |
| `unplanned` | Unscheduled stop or rate cut: fire, power loss, leak, mechanical failure, weather, strike, feedstock shortage | "shut", "tripped", "unplanned", "fire", "flaring following an upset", "taken offline after" |
| `planned` | Scheduled turnaround or maintenance announced in advance or described as routine | "turnaround", "scheduled maintenance", "planned work", "will be offline for about six weeks" |
| `restart` | A unit or plant returning to service after either of the above | "restarted", "back online", "resumed production", "ramping up" |

Not events, and never written to `outage_event`:

- Rate changes under 10 percent of unit capacity with no stated cause.
- Statements of intent with no date ("may consider maintenance later this year").
- Permanent closures and conversions. These go in `refinery.status` as a reference-data change,
  with a note in the decision log.
- Emissions events with no stated unit or throughput effect. These stay in `tceq_event` as a
  signal only.

## Capacity affected

`capacity_affected_kbd` is the nameplate throughput of the unit or units named, in thousand
barrels per day. Rules, in order:

1. If the source states a figure, use it, and keep the supporting quote.
2. If the source names a unit but not a figure, use the unit capacity from `refinery_unit` and
   set `confidence` to at most `medium`.
3. If the source names only the plant, use plant nameplate for `unplanned` or `planned` whole-
   plant events; for partial events with no unit named, write the row with capacity null and
   `confidence = low`. The note shows it as "capacity not stated".
4. Never exceed the nameplate of the unit or plant. Validation rejects it.

A partial rate cut is recorded as the amount cut, not the unit size. "Running at 60 percent" on
a 200 kb/d CDU is 80 kb/d affected.

## Dates

- `start_date`: the date the state changed, not the date it was reported. If only "last week"
  is given, use the Monday of that week and set `confidence` to at most `medium`.
- `expected_restart_date`: as stated, or null. "Several weeks" is null, not a guess.
- Weekday names resolve against the publication date: "Tuesday" in an article published on a
  Thursday is the Tuesday two days before. "Next Tuesday" is the one after. "Mid-October" is the
  15th; "early" and "late" are null.
- A restart that is announced ("plans to restart on Wednesday") is not a `restart` row. It is a
  revision: a new row that supersedes the open event and carries the new
  `expected_restart_date`. The `restart` row is written only when a source says the unit is back.
- Dates must fall within 90 days either side of the publication date. Outside that, the row goes
  to review.
- A `restart` row is a new row. It never writes to the event it ends. Whether an event is open or
  closed is worked out at query time, not stored. An event is closed as of T if a `restart` row
  for the same refinery and unit, known by T, matches it. Matching is by refinery and unit, with
  the most recent open event winning. The match is applied at query time.

## Revisions

A later source that changes any of unit, capacity, start or expected restart for an existing open
event produces a new row with `supersedes` set to the row it replaces. The old row is untouched.
It keeps its `known_at`, and no column on it is set or changed. Current rows are those no later
row supersedes. An as-of query at T sees only rows known by T, so it shows the old row until the
revision was known. The note shows "revised: restart 20 Nov, was 12 Nov".

## Confidence

| confidence | Conditions (all must hold) |
| --- | --- |
| `high` | Named refinery matched at 95 plus; named unit; capacity stated in the source or a company statement; two or more independent sources, or one company statement, or one regulator record plus one news source |
| `medium` | Named refinery matched at 90 plus; unit named or inferable; capacity from reference data; one reputable source |
| `low` | Anything else that passes validation: plant named but unit not, capacity not stated, single local-press source, GDELT lead not yet confirmed by a fetched article |

Confidence is computed in code from source kind, whether capacity was stated, match score and
corroboration count. The model never outputs it.

Regulator records (TCEQ, LDEQ) on their own are `low` and are shown in the note as signals, never
as capacity offline, until a news or company source confirms a throughput effect.

## Dedupe

Two extractions are the same event when the refinery matches, the unit matches (or one is null),
and start dates are within 3 days. The row with the higher confidence is kept; the others are
linked as corroboration. Corroboration count is what lifts `medium` to `high`.

## Regions

`US Gulf`, `US other`, `NW Europe`, `Med`, `Middle East`, `Singapore`, `NE Asia` (Japan, Korea,
Taiwan), `India`, `China`, `Other`. These are the groupings used in the note and the dashboard.
Do not add regions without a decision record.

## Open decisions

Found while labelling the first golden set. Each needs a decision record before the rule is coded.

1. **Old starts mentioned in passing.** The 90-day window rejects a March outage described in a
   July article. Options: keep strict and lose it; allow with `confidence = low` and a flag; widen
   to 180 days. Leaning: allow with `low`, because the row is still true and a desk would rather
   see it flagged than not at all.
2. **Null start date.** "Remains shut, restart undecided" with no start. Options: reject; accept
   with `valid_from` = publication date and a `start_unknown` flag. Leaning: accept, flagged.
3. **Reactivation of a closed plant.** A mothballed CDU returning is a `refinery.status` change,
   not a `restart`, by the rule above. Confirm this is what the note should show (as a reference
   change line, not in capacity returning).
4. **Partial sub-unit restarts with no figure.** Default: no row. Confirm.
