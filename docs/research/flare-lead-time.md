# Flare signal: method and limits

No FIRMS history has been loaded. The method is built and tested on synthetic data only. No hit
rate, false alarm rate or lead time is reported here.

Status: method built, no history loaded. The results file,
`docs/research/flare-lead-time-results.md`, does not exist yet. When history is loaded, the
`research` command writes it from SQL. Numbers live there and nowhere else.

Detections are from NASA FIRMS (VIIRS active fire). Plant outlines are from OpenStreetMap
contributors, under the ODbL.

## What this is

An in-house signal built from free public data. It scores daily satellite fire detections inside
each refinery's footprint against that plant's own 90-day normal. A flagged day is used in three
ways:

- as corroboration for an outage that a news or company source already reported (record 040);
- as an email alert when a plant runs far above normal (record 041);
- as the input to the measurements below.

It does not create events and it is never capacity. It does not replace a commercial flaring or
outage service.

## Limits

- **Cloud cover.** VIIRS sees through thin cloud at best. A cloudy day reads as zero detections.
  FIRMS area files carry no cloud mask, so "dark" and "not seen" look the same. This pulls
  baselines down in wet seasons and hides flaring under cloud.
- **Overpass times.** Two satellites, at roughly 01:30 and 13:30 local solar time each. A flare
  that lasts two hours between passes is missed. Detections arrive about 3 hours after the pass.
- **Small flares.** A 375 m pixel needs enough heat to trigger. Routine small flares and enclosed
  ground flares are often not seen at all.
- **False positives.** Fires near the fence line, adjacent petrochemical plants inside the 375 m
  buffer, gas flares at nearby terminals, and planned flaring during a turnaround all score.
  Approximate footprints make this worse. They are flagged in every output.
- **Not designed for flares.** The FIRMS active-fire product finds fires. Products built for gas
  flares exist elsewhere. This uses FIRMS because it is free, documented and has an API.
- **Quiet days are not truly quiet.** An outage we never saw counts as a false alarm, so the false
  alarm rate is an upper bound. Planned flaring counts the same way.
- **Thin history.** Outage events exist only from go-live. The Wilson interval shows how little a
  handful of events says.
- **Backfilled lead times rest on an assumption.** `available_at` adds 3 hours to the last pass of
  the day. Real latency varies. The results file reports the backfilled share.
- **Baseline drift.** A satellite outage, a footprint change or a new satellite changes the
  detection rate. A footprint change starts a new row but not a new baseline.

## Data

Suomi NPP and NOAA-20, through the FIRMS area API (record 038). For each plant, day and satellite
the standard product (SP) is used if it was fetched, otherwise the near-real-time product (NRT).
A day is scored only when both satellites were fetched for it. NOAA-21 is left out.

A plant's footprint is an OpenStreetMap polygon that contains the operator's point, or a circle
round the point when none does (record 037). A detection counts if it is inside the footprint or
within 375 m of its edge, which is one pixel. Every footprint that is not an exact polygon is
flagged approximate.

## Score

For a plant and a UTC day, `x = ln(1 + FRP sum in MW)`. The baseline is the median and the MAD of
`x` over the scored days from 90 days before to the day before (record 039). Days with no row are
missing, never zero. A day needs 60 scored days in its window or it gets no score.

```
z = (x - median) / greatest(1.4826 * MAD, 0.5)
```

The floor means a plant that is usually dark needs about 11 MW in a day to reach z = 5. The score
and baseline are stored on the row as known when it was written. A later revision of an old day
does not rescore later days. All of this is SQL: `flare_day_write` in migration r0009.

Thresholds are rows in `flare_threshold`. Until calibrated, signal is z of 3.5 with at least 1
detection and alert is z of 6.0 with at least 2. Calibration picks the lowest z on a 0.25 grid from
3.0 to 10.0 whose false alarm rate is at or under a target: 1.0 per plant-month for signal, 0.2 for
alert. It refuses with fewer than 1,000 quiet scored plant-days.

## Definitions

All as of a time T. A row counts only if it was known by T.

- **Scored plant-day:** a current `flare_day` with a score.
- **Flagged:** a scored plant-day with `z >= z_min` and `n_detections >= min_detections`, for the
  threshold in force at T.
- **Event set:** current `unplanned` outage rows, at a plant with a footprint, whose start date is a
  scored plant-day. `planned` rows are counted but left out of the hit rate, because they are
  announced ahead.
- **First report:** the earliest publication time, or fetch time when there is none, of any source
  of any row in the event's chain.
- **Hit:** a flagged day at that plant from 3 days before to 3 days after the start date.
- **Lead time:** first report minus the earliest `available_at` among the hit's flagged days, in
  hours. Negative means the news came first. The median is `percentile_cont(0.5)`.
- **Quiet plant-day:** a scored plant-day with no outage row start and no regulator item at the
  plant within 7 days either side.
- **False alarm rate:** flagged quiet plant-days divided by quiet plant-days over 30.4375.
- **Hit rate interval:** Wilson 95 percent, computed in SQL.

## SQL

The figures come from `src/refinery_watch/flare/research.py`. The core of it is below.

Flagged days and the earliest availability for each event:

```sql
SELECT min(f.available_at)
FROM days f
WHERE f.is_flagged AND f.refinery_id = e.refinery_id
  AND f.day BETWEEN e.start_date - 3 AND e.start_date + 3
```

Quiet days and the false alarm rate:

```sql
WITH quiet AS (
  SELECT d.z, d.n_detections
  FROM flare_day_current_as_of(:as_of) d
  WHERE d.z IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM outage_current_as_of(:as_of) o
                    WHERE o.refinery_id = d.refinery_id
                      AND o.start_date BETWEEN d.day - 7 AND d.day + 7)
    AND NOT EXISTS (SELECT 1 FROM tceq_event t
                    WHERE t.refinery_id = d.refinery_id AND t.known_at <= :as_of
                      AND (coalesce(t.started_at, t.known_at) AT TIME ZONE 'UTC')::date
                          BETWEEN d.day - 7 AND d.day + 7)
)
SELECT count(*) FILTER (WHERE z >= :z_min AND n_detections >= :min_detections)
         * 30.4375 / nullif(count(*), 0) AS false_alarms_per_plant_month
FROM quiet;
```

Wilson 95 percent bounds for `h` hits in `n` events, with `p = h / n` and `k = 1.96`:

```
low  = (p + k^2 / (2n) - k * sqrt(p * (1 - p) / n + k^2 / (4n^2))) / (1 + k^2 / n)
high = (p + k^2 / (2n) + k * sqrt(p * (1 - p) / n + k^2 / (4n^2))) / (1 + k^2 / n)
```

Both are undefined when `n` is 0. The SQL uses `nullif(n, 0)`, so they come out empty.

## Running it

```
python -m refinery_watch.flare research --out docs/research/flare-lead-time-results.md
python -m refinery_watch.flare calibrate --kind signal
python -m refinery_watch.flare calibrate --kind alert
```

`research` writes the results file only. It never edits this document. `calibrate` prints the
curve and writes a threshold row only with `--write`. See the runbook for the order of the steps.
