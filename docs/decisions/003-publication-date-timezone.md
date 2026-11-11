# 003: Publication date in the source's timezone

Date: 2026-11-11

## Context

Relative dates in a source ("Tuesday", "last week", "will restart Thursday") and the 90-day check
in the Dates section of docs/definitions.md all resolve against the publication date. A source
publishing early in the morning east of UTC is on a different calendar day from UTC. A Singapore
article from 07:00 SGT on Friday 13 Nov is 23:00 UTC on Thursday 12 Nov. If we use the UTC date,
"will restart Thursday" lands on 12 Nov, a week early.

## Options

- Always the UTC date. Simple and needs no timezone data. Wrong by a day for early-morning
  articles east of UTC and late-evening articles west of it, and that error can move a weekday by
  a whole week.
- The date in the source's own timezone where the source states one, otherwise UTC. Matches
  what the writer meant. The cost is keeping a timezone where the source gives one, and a fallback
  that is still wrong for sources that give none.

## Decision

The publication date is the calendar date in the source's own timezone, where the source states
one. Otherwise it is the UTC date. Every relative date and the 90-day check resolve against it.
Stored timestamps stay UTC. Only the date used for resolving changes.

## Consequences

Weekday and "last week" dates match what the article said, so a refined products desk does not
see a restart a week off. The fetch step has to keep the stated timezone with each article, and
tests need cases on both sides of midnight UTC. Sources with no timezone still fall back to UTC
and can be a day off. Whether the timezone is stored per source or per article is left to the
`outage_event` schema spec. To undo, resolve against the UTC date and drop the stored timezone.
