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

- The publication date is the calendar date in the source's own timezone, where the source
  states one. Otherwise it is the UTC date. Every relative date below, and the 90-day check,
  resolves against it. Stored timestamps stay UTC. A Singapore article published at 07:00 SGT on
  Friday 13 Nov has publication date 13 Nov, although it is 23:00 UTC on 12 Nov.
- `start_date`: the date the state changed, not the date it was reported. If only "last week"
  is given, use the Monday of that week and set `confidence` to at most `medium`.
- `expected_restart_date`: as stated, or null. "Several weeks" is null, not a guess.
- Weekday names resolve against the publication date. The tense of the sentence sets the
  direction.
  - Past or present tense ("shut on Tuesday", "has been offline since Tuesday") resolves to the
    most recent such day on or before the publication date. "Tuesday" in an article published on
    a Thursday is the Tuesday two days before.
  - Future tense ("plans to", "will", "expected to") resolves to the next such day on or after
    the publication date. "Plans to restart on Wednesday" in an article published on a Thursday
    is the Wednesday six days after.
  - In both cases, a weekday that names the publication day resolves to the publication date
    itself.
  - "Next Tuesday" is the first Tuesday after the publication date, never the publication date
    itself. "Last Tuesday" is the most recent Tuesday before the publication date, never the
    publication date itself.
  - If the tense is unclear, the row goes to review.
- "Mid-October" is the 15th; "early" and "late" are null.
- A restart that is announced ("plans to restart on Wednesday") is not a `restart` row. It is a
  revision: a new row that supersedes the open event and carries the new
  `expected_restart_date`. The `restart` row is written only when a source says the unit is back.
- Dates must fall within 90 days either side of the publication date. Outside that, the row goes
  to review.

## Restarts

These rules are provisional. They are settled in the `outage_event` schema spec, where they are
tested against a seeded database.

A `restart` row is a new row. It never writes to the event it ends. On a `restart` row,
`start_date` is the date the unit came back. If that date is not known, the restart goes to
review. The row carries `ends_event`, a new column: the id of the event row it ends.
`ends_event` is set once, when the restart row is written, and is null on `unplanned` and
`planned` rows.

To set `ends_event`, look only at rows current at the restart's `known_at`. Of those, take the
`unplanned` and `planned` rows that:

- are for the same refinery and unit. A null unit equals a null unit, so a whole-plant restart
  matches a whole-plant outage. A null unit never equals a named unit (see open decision 5);
- are open at that time;
- have a `start_date` on or before the restart's own `start_date`.

The one with the latest `start_date` wins. A tie goes to the later `known_at`, then to the lower
id. If no row qualifies, the restart is not written to `outage_event`. It goes to review.

Matching happens once, at write time. It never runs again at query time, so a later event cannot
take an earlier restart. A restart reported late still ends the event it follows, not one that
started after the unit came back.

An event is closed as of T if a `restart` row current as of T has `ends_event` pointing at the
event's row or at any row in its `supersedes` chain. Otherwise it is open. A revision written after
the restart is in the same chain, so the event stays closed.

Example: a CDU goes down on 1 Nov (E1). A restart is known on 5 Nov (X1), with `ends_event` set to
E1. The CDU goes down again on 20 Nov (E2). As of 21 Nov, E1 is closed and E2 is open. If instead
X1 were first reported on 25 Nov with the unit back on 10 Nov, it would still end E1, because E2
started after 10 Nov.

## Revisions

A later source that changes any of unit, capacity, start or expected restart for an existing
event, open or closed, produces a new row with `supersedes` set to the row it replaces. The old
row is untouched. It keeps its `known_at`, and no column on it is set or changed. Current rows are
those no later row supersedes. An as-of query at T sees only rows known by T, so it shows the old
row until the revision was known. The note shows "revised: restart 20 Nov, was 12 Nov".

- A revision of a closed event, for example a capacity figure published after the restart, is in
  the same chain as the row the restart points at. The event stays closed.
- A revision of a `restart` row copies `ends_event` from the row it supersedes. It never matches
  again. A revision that changes the refinery or unit of a restart, or moves its date before the
  `start_date` of the event it ends, goes to review.
- A new row may supersede only a row that is current when the new row is written. A second row
  that supersedes the same row goes to review. Each chain is then a single line with at most one
  current row at any T. This keeps the as-of rule as written: one event never shows two current
  rows, and its capacity is never counted twice. The database can hold this with a unique
  constraint on `supersedes`.

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

These rules are provisional. They are settled in the `outage_event` schema spec, where they are
tested against a seeded database.

Two extractions are the same event when they are in the same family, the refinery matches, the
unit matches (or one is null), and start dates are within 3 days. There are two families:
outages (`unplanned` and `planned`) and restarts (`restart`). A shutdown and its own restart are
never one event. One outage that one source calls planned and another calls unplanned is one
event, so its capacity is counted once. The row with the higher confidence is kept, with its event
type; the others are linked as corroboration. Corroboration count is what lifts `medium` to
`high`.

Not settled: whether dedupe runs on extractions before they reach `outage_event`, or on rows
already in it. If it runs after, "kept" and "linked" cannot mean changing or removing a row,
because rows in `outage_event` are never updated or deleted. This needs a decision record before
dedupe is coded.

## Regions

`US Gulf`, `US other`, `NW Europe`, `Med`, `Middle East`, `Singapore`, `NE Asia` (Japan, Korea,
Taiwan), `India`, `China`, `Other`. These are the groupings used in the note and the dashboard.
Do not add regions without a decision record.

## Open decisions

Decisions 1 to 4 were found while labelling the first golden set; decisions 5 to 7 came from
review of the restart and dedupe rules. Each needs a decision record before the rule is coded.

1. **Old starts mentioned in passing.** The 90-day window rejects a March outage described in a
   July article. Options: keep strict and lose it; allow with `confidence = low` and a flag; widen
   to 180 days. Leaning: allow with `low`, because the row is still true and a desk would rather
   see it flagged than not at all.
2. **Null start date.** "Remains shut, restart undecided" with no start. Options: reject; accept
   with `start_date` set to the publication date and a `start_unknown` flag. Leaning: accept,
   flagged. `known_at` is not affected either way. The record must also say whether a placeholder
   `start_date` takes part in the 3-day dedupe window and in restart matching.
3. **Reactivation of a closed plant.** A mothballed CDU returning is a `refinery.status` change,
   not a `restart`, by the rule above. Confirm this is what the note should show (as a reference
   change line, not in capacity returning).
4. **Partial sub-unit restarts with no figure.** Default: no row. Confirm.
5. **Whole-plant restart with unit outages open.** "The refinery is back online" while named units
   at that refinery are open. `ends_event` holds one id, so as written the restart matches only an
   open whole-plant outage, and goes to review if there is none. The same applies in reverse: a
   unit restart never ends a whole-plant outage. Options: keep this; write one `restart` row per
   open unit; let a whole-plant restart close every open event at that refinery, which needs a
   different column than a single `ends_event`.
6. **Two open outages on one unit.** A new outage on a unit that already has an open outage leaves
   both open. The unit counts offline twice, and the first outage may never close. Intended rule:
   a unit has at most one open outage at a time. A new shutdown on a unit with an open outage
   either supersedes it or goes to review, never both open. The schema enforces this with a
   constraint, not only in code.
7. **Dedupe across a restart.** The 3-day window ignores a restart in between. Restarts of two
   different outages merge, and a re-trip after a restart merges back into the first outage.
   Intended rule: dedupe never merges two rows with a restart between them, and restarts dedupe
   only by the event they end.
