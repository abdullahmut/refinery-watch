# 041: Instant flare alerts, one per plant per day, through a shared mail module

Date: 2026-11-12

## Context

A high score should reach the desk before the morning note. Cron can run a job twice, a send can
time out after Resend accepted it, and a plant can stay hot for days. No mail client exists yet;
feat/note planned its own Resend client under `note/`. The mail key lives only on the app Droplet
(record 031). Resend's free plan has a daily email limit. Check the current plan.

## Options

- A `status` column on the alert row, updated after the send. Simple, but it needs UPDATE on a
  signal table.
- A claim row inserted before the send, unique per plant per Dubai day and per plant per flare
  day, plus an append-only attempt row for each send, with a stored `Idempotency-Key`.
- One digest email per run listing every hot plant. Fewer emails, but one failed send holds back
  every plant, and the key covers a payload that changes as plants are added.

## Decision

The claim and attempt rows. `flare_alert` has two unique indexes: `(refinery_id, alert_day)`, where
`alert_day` is the Asia/Dubai date of `built_at`, and `(refinery_id, flare_date)`. The row stores
subject, bodies, recipient fingerprint and idempotency key. A retry resends the stored payload with
the same key, at most 3 attempts and only within 18 hours of `built_at`. There is a cap of
`FLARE_ALERT_DAILY_MAX` alerts per Dubai day across all plants, default 10, checked in code under an
advisory lock. The Resend client moves to `refinery_watch/mail/resend.py` with the interface
feat/note specified, so the note imports it. The alert job runs on app. Fetching runs on ingest.

## Consequences

The database, not the code, stops a second alert for a plant on one day. Backfilled and old days
never alert. A run without `--send` builds the alerts and prints them, but writes nothing and sends
nothing. feat/note drops its own client and config commits and imports the shared ones. To undo,
remove the alert cron line. The tables stay.
