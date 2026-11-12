# Architecture

## Purpose

refinery-watch keeps a point-in-time history of refinery outages and a satellite flare signal for
a refined products desk. It reads public news and regulator feeds. The plan is to extract outage
events with an LLM and send a sourced morning note. It is built to run unattended.

Nothing is deployed. No live data is loaded.

## Status

Built:

- Postgres schema and migrations, with the append-only outage store and `outage_as_of`.
- Seed loaders for sources and refineries.
- Source adapters: Hydrocarbon Processing RSS, GDELT DOC, EIA weekly utilisation.
- The keyword pre-filter and refinery name matching.
- The flare signal, tested on synthetic data and hand-made fixtures.
- Terraform and cloud-init for three Droplets. Compose for the data Droplet.

Specified, not built:

- LLM extraction and the spend cap.
- The morning note and its email.
- Scheduling, monitoring and backups.
- Deploy.

## Droplets

DigitalOcean, London region, one VPC. Cloud firewalls allow SSH on a non-default port from listed
addresses, 443 on app only, and Postgres on data from the VPC only.

| Droplet | RAM | Role | Runs |
| --- | --- | --- | --- |
| ingest | 512 MB | fetch | source fetch jobs and the flare fetch |
| data | 1 GB | store | Postgres |
| app | 1 GB | serve | dashboard and read API behind Caddy; flare alerts; the only Droplet exposing 443 |

Only the data Droplet runs a Compose stack today. The app and ingest stacks are specified, not
written.

## Database roles

Two roles, split so the app cannot change the schema.

- `refinery_app` is used by every job and the API. It has SELECT and INSERT on the append-only
  tables and no UPDATE or DELETE. Jobs read `PG_HOST`, `PG_APP_USER` and `PG_APP_PASSWORD`, or
  `APP_DATABASE_URL`.
- `refinery_owner` runs migrations and seeds. Its password is in the data Droplet's `.env`, for
  Postgres init only. It is never in the ingest or app `.env`. The operator supplies it in the
  shell when running migrate or seed.

See [014](decisions/014-migrations-and-test-database.md) and the "How each job connects" entry in
[the runbook](runbook.md).

## Storage

Postgres 16 is the only database. It runs in Compose on the data Droplet, bound to the VPC
address.

Raw documents and backups go to Cloudflare R2, over the S3 API
([029](decisions/029-cloudflare-r2-object-store.md)). R2 replaces the MinIO plan in
[008](decisions/008-postgres-and-minio.md). Two buckets:

- raw, at `raw/{source}/{yyyy}/{mm}/{dd}/{sha}.{ext}`, where `ext` is `html` for article pages
  and `json` for API responses. Ingest writes. App reads.
- backup, for the nightly dump. Only the data Droplet holds its key.

No object store container runs on any Droplet.

## Memory

| Where | Container | Limit |
| --- | --- | --- |
| data | postgres | 384 MB |
| ingest | one job at a time, flare fetch | 192 MB |
| app | flare alert job | 128 MB |

Postgres is tuned to fit its limit: `shared_buffers` 96 MB, `max_connections` 20, no parallel
workers. About 640 MB of the data Droplet is left for the OS and Docker. Limits for the other
containers are not set yet. Each container's cost is written next to its Compose file when the
stack is written.

## Data flow

Steps 1 to 3 are built. The rest are specified.

1. Fetch from public news and regulator feeds. Respect robots.txt and rate limits, send a plain
   User-Agent with a contact email, back off on 429 and 5xx, never evade a block. A source row with
   a robots status that is not a clear allow is refused.
2. Hash normalised text with SHA-256. Never process the same hash twice.
3. Write the raw document to R2 and a `raw_document` row to Postgres.
4. Pre-filter with keywords before any model call.
5. Check the daily spend cap.
6. Extract with Claude Haiku. Log tokens and cost on every call.
7. Validate. Failures go to review and never reach `outage_event`.
8. Write to `outage_event`.
9. At 06:00 Asia/Dubai, build the note. Every number comes from SQL. Claude Sonnet writes the
   two-sentence summary and nothing else. Email it.
10. The dashboard reads through the API, never the database.

## Flare signal

A separate append-only store, so a satellite signal never sits next to capacity
([036](decisions/036-flare-signal-store.md)). Tables from migration r0009:

| Table | Holds |
| --- | --- |
| `plant_footprint` | outline per plant: OSM polygon, OSM bounds, or a flagged point and radius |
| `flare_detection` | VIIRS detections inside a footprint, per satellite (SNPP, NOAA-20) and product |
| `flare_coverage` | which plant, day, satellite and product were fetched |
| `flare_day` | daily count, radiative power sum, baseline and z score, with `known_at` and `available_at` |
| `flare_threshold` | the signal and alert thresholds, each with its `known_at` |
| `flare_alert` | the built alert email, one per plant per Dubai day |
| `flare_alert_attempt` | each send attempt, up to 3 |

The app role has SELECT and INSERT only on these. Nothing here writes to `outage_event` or
`event_source`. A flare lifts a `low` confidence to `medium` at query time and no further
([040](decisions/040-flare-confidence-rule.md)). Source and score: records
[037](decisions/037-plant-footprints.md) to [041](decisions/041-flare-alerts-and-shared-mail.md).
Method and limits: [flare-lead-time.md](research/flare-lead-time.md).

## Rules that hold everywhere

- `outage_event` is append-only. UPDATE and DELETE on it are revoked at the database level. A
  revision is a new row with `supersedes` pointing at the row it replaces. The old row is never
  marked. Current rows are those no later row supersedes. An as-of query must see only what was
  known at time T.
- Hard daily spend cap of 1.50 USD, enforced in code.
- Every number in the morning note comes from SQL. The model writes prose only.
- Store UTC. Display Asia/Dubai in the note and say so in the footer. 06:00 Asia/Dubai is 02:00 UTC
  all year, because Dubai has no daylight saving.
- Flaring and regulator records are signals. They are never capacity.
- [docs/definitions.md](definitions.md) is the single source for event types, capacity, dates and
  confidence.

## Undecided

- Whether the spend cap day runs on UTC or Asia/Dubai.
- What the 06:00 note does if the cap is already spent.
- How deploys reach the VMs.
- Memory limits on app and ingest, beyond the two jobs above.
- The open decisions listed in [docs/definitions.md](definitions.md).
