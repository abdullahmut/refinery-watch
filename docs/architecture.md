# Architecture

## Purpose

refinery-watch is a refinery outage tracker built to run unattended. It reads public news and
regulator feeds, uses an LLM to extract outage events, and produces a sourced morning note and a
dashboard.

## Droplets

DigitalOcean, London region, one VPC.

| Droplet | RAM | Runs |
| --- | --- | --- |
| ingest | 512 MB | source fetch jobs |
| data | 1 GB | Postgres and MinIO |
| app | 1 GB | dashboard and read API behind Caddy; the only Droplet exposing 443 |

## Storage

Postgres is the only database. MinIO holds raw documents at
`raw/{source}/{yyyy}/{mm}/{dd}/{sha}.html`.

## Data flow

1. Fetch from public news and regulator feeds. Respect robots.txt and rate limits, send a plain
   User-Agent with a contact email, back off on 429 and 5xx, never evade a block.
2. Hash normalised content with SHA-256. Never process the same hash twice.
3. Write the raw document to MinIO and a `raw_document` row to Postgres.
4. Pre-filter with keywords before any model call.
5. Check the daily spend cap.
6. Extract with Claude Haiku. Log tokens and cost on every call.
7. Validate. Failures go to review and never reach `outage_event`.
8. Write to `outage_event`.
9. At 06:00 Asia/Dubai, build the note. Every number comes from SQL. Claude Sonnet writes the
   two-sentence summary and nothing else. Email it.
10. The dashboard reads through the API, never the database.

## Rules that hold everywhere

- `outage_event` is append-only. UPDATE and DELETE on it are revoked at the database level. A
  revision is a new row with `supersedes` pointing at the row it replaces. The old row is never
  marked. Current rows are those no later row supersedes. An as-of query must see only what was
  known at time T.
- Hard daily spend cap of 1.50 USD, enforced in code.
- Every number in the morning note comes from SQL. The model writes prose only.
- Store UTC. Display Asia/Dubai in the note and say so in the footer. 06:00 Asia/Dubai is 02:00 UTC
  all year, because Dubai has no daylight saving.
- [docs/definitions.md](definitions.md) is the single source for event types, capacity, dates and
  confidence.

## Undecided

- Which Droplet runs extraction and the note job: undecided.
- Whether cron runs on the host or in a container: undecided. Cron itself is settled.
- Whether the spend cap day runs on UTC or Asia/Dubai: undecided.
- What the 06:00 note does if the cap is already spent: undecided.
- Which service sends the note: undecided.
- Memory limits per container, including how Postgres and MinIO share 1 GB: undecided.
- How deploys reach the VMs: undecided.
- Where the second backup copy lives: undecided. It must be off the data Droplet to survive losing
  it.
- Where Terraform state lives: undecided.
- The four open decisions listed in [docs/definitions.md](definitions.md): undecided.
- Open for the `outage_event` schema spec. A `supersedes` column on the new row points at the row
  it replaces. UPDATE and DELETE are revoked at the database level. Current rows are those no later
  row supersedes. `docs/definitions.md` now describes restarts and revisions as new rows, with
  open and closed state derived at query time.
