# 039: The flare anomaly score and how its thresholds are set

Date: 2026-11-12

## Context

Each plant has its own normal. Some flare every night; some show nothing for months. Daily fire
radiative power (FRP) is heavy-tailed, and many plants have mostly zero days. The threshold has to
come from data, but there is no history yet.

## Options

- A plain z-score on FRP. One past episode inflates the mean and the spread, and hides the next.
- Median and MAD on log FRP. Robust to past episodes, but the MAD is zero for a plant that is
  usually dark, so any detection scores infinity.
- A percentile rank against the plant's own 90 days. Simple, but it tops out at 100, so it cannot
  say how far above normal a day is, which the alert must say.
- A count model such as Poisson. Needs fitting code and still struggles with clustered detections.

## Decision

Median and MAD on `x = ln(1 + FRP sum in MW)`, over the 90 scored days before the day, with a floor
on the scale: `z = (x - median) / max(1.4826 * MAD, 0.5)`. A day needs 60 scored days in its window
or it gets no score. The floor means a dark plant needs about 11 MW in a day to reach z = 5.
Thresholds live in `flare_threshold`, append-only, two kinds. Defaults until calibrated: signal
z >= 3.5 with at least 1 detection, alert z >= 6.0 with at least 2 detections. The calibrate
command takes a target false-alarm rate per plant-month (signal 1.0, alert 0.2). It treats a
scored plant-day as quiet when no outage or regulator item at that plant starts within 7 days.
It picks the lowest z on a 0.25 grid from 3.0 to 10.0 that meets the target. It refuses with fewer
than 1,000 quiet scored plant-days, and writes a row only with `--write`.

## Consequences

All the arithmetic is SQL, so every figure in an alert or the research comes from a query. The
baseline and z are stored on the row as known when it was written, so a later revision of an old
day does not rescore later days. Defaults are guesses and are labelled `default` in every output
until a calibrated row exists. To change the method, add a record and a new score column; old rows
keep the score they were written with.

The two default threshold rows are stored with a known time of 1970-01-01, so they apply at every
as-of time. A calibrated row applies only from the time it was written. In an alert, "times
normal" is (1 + the day's FRP sum) divided by (1 + the baseline median FRP), which is
`exp(x - median)` on the stored values. It is left out when the median is under 1 MW.
