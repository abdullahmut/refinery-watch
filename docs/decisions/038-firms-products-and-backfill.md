# 038: Which FIRMS data, and how history is loaded

Date: 2026-11-12

## Context

NASA FIRMS serves VIIRS active-fire detections from three satellites. Each has a near-real-time
product (NRT) and, after a lag of some months, a standard product (SP). A baseline needs 90 days of
history before the first score, and the research needs a year. Backfilled rows are inserted today,
so their `known_at` says nothing about when the data could first have been seen.

## Options

- All three satellites. NOAA-21 has a shorter history than the other two, so the detection rate
  would change partway through a baseline.
- Suomi NPP and NOAA-20 only, SP where it exists and NRT after, one product per satellite per day.
- The FIRMS bulk archive by email request. That is a manual download outside the fetcher, with no
  robots or rate checks.

## Decision

Suomi NPP and NOAA-20, through the FIRMS area API, by the shared fetcher. The backfill reads the
FIRMS data availability list first and asks for SP up to its last date, then NRT. For each plant,
day and satellite, SP is used if it was fetched, otherwise NRT. A day is scored only when both
satellites were fetched for it. Backfilled `flare_day` rows set `backfilled = true` and
`available_at` to the last detection time that day plus 3 hours, a stated assumption about NRT
latency. Live rows set `available_at = known_at`. Backfilled rows never raise an alert.

## Consequences

The baseline compares like with like. The research can measure lead time on history, but those
lead times rest on the 3-hour assumption, and the write-up says so. The backfill is about 1,500
requests for a year and resumes where it stopped, so the operator runs it in short batches. Adding
NOAA-21 later is a new satellite value and a fresh baseline, which needs its own record.
