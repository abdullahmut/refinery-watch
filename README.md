# refinery-watch

This README describes main after the branches merge in order: infra/terraform,
infra/data-compose, feat/schema, feat/adapters, feat/flare-signal, then the docs branches.

## What this is

An in-house data layer for a refined products desk, on top of its vendor data. It
keeps a point-in-time history of refinery outages, so a backtest sees only what was known on the
day, and it adds alt-data signals such as satellite flaring.

It does not replace a commercial outage service. Its limits:

- Coverage is whatever public sources report. Operators often say nothing, or say it late.
- Capacity figures are as reported. Many reports give none, and the row says so.
- It is one developer's project, not a vendor product. No history is loaded yet.
- Point-in-time history starts at the first live write. See the point-in-time section.

## What is built today

- An append-only outage store in Postgres, with `outage_as_of(T)` queries.
- Source adapters for the Hydrocarbon Processing RSS feeds, GDELT DOC and EIA weekly refinery
  utilisation. Each fetch checks robots.txt and a per-source status gate before it runs. As seeded,
  only the HP and EIA jobs are cleared to run. GDELT and both flare sources (FIRMS and Overpass) stay off until
  their robots status is checked by hand.
- The keyword pre-filter and refinery name matching.
- The VIIRS flare signal, on synthetic data. The fetch code is tested against hand-made fixtures.
  Flare alert sending through Resend is code only and has never been run.
- Terraform for the three Droplets. Compose for the data Droplet.

Nothing is deployed. No live data is loaded. The flare method has not been run on real FIRMS
history, so it reports no hit rate or lead time. See
[docs/research/flare-lead-time.md](docs/research/flare-lead-time.md).

## Specified but not built

- LLM extraction of events from article text.
- The morning note.
- Scheduling and monitoring.
- Backups: the nightly dump, the second copy and the restore script.
- Deploy.

No regulator data is ingested yet. The `tceq_event` table exists and nothing writes to it.

## How it works

**Sources.** One small fetcher serves every adapter. It reads robots.txt, keeps to a rate limit,
backs off on 429 and 5xx, and never works round a block. Each source row carries a robots status,
and the job refuses to run unless that status is a clear allow
([015](docs/decisions/015-source-fetch-rules.md)). GDELT is used for English leads only
([016](docs/decisions/016-gdelt-doc-api-for-leads.md)). EIA comes through API v2
([017](docs/decisions/017-eia-api-v2.md)). Reading Hydrocarbon Processing is reference use
([035](docs/decisions/035-hp-reading-is-not-ai-training.md)).

**Dedupe.** The content hash is SHA-256 over normalised text, not page bytes, so a re-skinned page
is the same document ([018](docs/decisions/018-content-hash-covers-text.md)). Events are checked
against open events before the write
([010](docs/decisions/010-dedupe-before-write.md)). Names in article text are matched to refineries
by score ([022](docs/decisions/022-refinery-matching.md)).

**Outage store.** `outage_event` takes inserts only. UPDATE and DELETE are revoked and a trigger
refuses them. One open outage per unit is held by an insert trigger
([009](docs/decisions/009-one-open-outage-per-unit.md)). A whole-plant restart ends only
whole-plant outages ([011](docs/decisions/011-whole-plant-restarts.md)). Old and unknown start dates
are flagged, not guessed ([012](docs/decisions/012-old-and-unknown-start-dates.md)). Migrations
only go forward ([014](docs/decisions/014-migrations-and-test-database.md)). Terms are in
[docs/definitions.md](docs/definitions.md).

**Flare signal.** FIRMS detections inside each plant's footprint are scored against that plant's own
90-day normal. Footprints come from OpenStreetMap, with a flagged circle where none exists
([037](docs/decisions/037-plant-footprints.md)). The store is separate
([036](docs/decisions/036-flare-signal-store.md)). Data choice and backfill are in
[038](docs/decisions/038-firms-products-and-backfill.md), the score in
[039](docs/decisions/039-flare-anomaly-score.md), alerts in
[041](docs/decisions/041-flare-alerts-and-shared-mail.md).

**Infrastructure.** Three DigitalOcean Droplets in London on one VPC
([007](docs/decisions/007-why-digitalocean.md)). Postgres runs on the data Droplet. Raw documents
go to Cloudflare R2 ([029](docs/decisions/029-cloudflare-r2-object-store.md)), chosen but not yet
written to. Backups are planned for the same store.
Terraform state is local ([006](docs/decisions/006-terraform-state-location.md)). Layout and
memory limits are in [docs/architecture.md](docs/architecture.md).

## Point-in-time rule

Every row carries `known_at`, the time the system learned it. A revision is a new row whose
`supersedes` points at the row it replaces. The old row is never marked. Current rows are those no
later row supersedes. `outage_current_as_of(T)` applies that test with `known_at <= T` on both
sides, so a revision written after T cannot change the answer for T. `outage_as_of(T)` is the
open-outage view on top of it: current unplanned and planned rows that no restart has ended. The
tests cover this ([tests/db/test_outage_as_of.py](tests/db/test_outage_as_of.py)).

Limit: `known_at` always comes from the database clock and must be later than every existing row.
Past outages cannot be loaded with a past `known_at`. Point-in-time history starts at the first
live write. A backtest needs history collected from that date on.

Dates are stored in UTC.

## Signals against capacity

Flaring and regulator records are signals. They are never capacity. A flare cannot name a unit or
say how much was lost, so it creates no event and adds no barrels. At most it lifts a `low`
confidence row to `medium` at query time, when every other condition for `medium` already holds
([040](docs/decisions/040-flare-confidence-rule.md)). The flare rule never lifts a row whose
only sources are regulator sources.

## Running it locally

Needs GNU Make 4, uv, Docker and bash. On Windows use Git Bash.

```
make test               # unit tests, no database
make test-integration   # the whole suite, needs the local Postgres from make up
make lint
make tf-check           # needs Terraform
make up                 # Postgres on 127.0.0.1:5432
make down
```

Copy `.env.example` to `.env` before `make up`. It lists every key the code reads, with empty
values. The local stack needs `POSTGRES_PASSWORD`, `PG_OWNER_PASSWORD` and `PG_APP_PASSWORD`. The
`OBJECT_STORE_BACKUP_*` keys are for the backup job, which is not built yet. Never commit `.env`.

Every key `config.py` reads:

- Database, app role: `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_APP_USER`, `PG_APP_PASSWORD`, or
  `APP_DATABASE_URL` instead of the others.
- Database, owner role, for migrate and seed only: `PG_OWNER_USER`, `PG_OWNER_PASSWORD`, or
  `OWNER_DATABASE_URL`. Type these in the shell. On the Droplets, the owner password is stored
  only in the data Droplet's `.env`.
- Objects: `OBJECT_STORE_ENDPOINT`, `OBJECT_STORE_REGION`, `OBJECT_STORE_BUCKET_RAW`,
  `OBJECT_STORE_ACCESS_KEY`, `OBJECT_STORE_SECRET_KEY`.
- Sources: `CONTACT_EMAIL`, `EIA_API_KEY`, `FETCH_IPV6`, `FIRMS_MAP_KEY`.
- Files: `REFINERIES_CSV_PATH` and `REFINERY_LOCATIONS_CSV_PATH`. The refinery list is an operator
  file, not in `data/`.
- Mail: `NOTE_RECIPIENTS`, `NOTE_FROM`, `RESEND_API_KEY`, `FLARE_ALERT_DAILY_MAX`.

## Repository map

- `src/refinery_watch/db/` connection, migrations, seed loaders and event writes.
- `src/refinery_watch/sources/` fetcher, robots, and the three adapters.
- `src/refinery_watch/extract/` keyword pre-filter and refinery matching.
- `src/refinery_watch/flare/` footprints, FIRMS fetch, score, alerts.
- `src/refinery_watch/mail/` mail sender.
- `src/refinery_watch/config.py` settings from the environment. `objects.py` S3 storage for raw
  documents. `dates.py` publication date handling. `terms.py` keyword lists.
- `data/` source list and unit-to-product map.
- `infra/terraform/`, `infra/cloud-init/`, `infra/compose/` the three Droplets.
- `tests/` mirrors `src/`. Tests marked `integration` need Postgres.
- `docs/` architecture, definitions, runbook, research and decision records.

## Known open issues

After the merges there are 10 tests marked `xfail(strict=True)`. Each states a defect that is not
fixed yet. A strict xfail fails the run if the defect disappears, so the mark cannot go stale.
The schema descriptions below paraphrase the reasons in `tests/db/test_open_issues.py`.

Schema, in `tests/db/test_open_issues.py`:

- The one-day UTC slack lets a restart dated tomorrow close today, because the source's own
  publication date is not used (rule RW015).
- The restart lookup has no date window, so a restart dated before a re-trip is absorbed into the
  older restart.
- `outage_cutoff` has a 5 s lock timeout, so a slow writer makes the note job fail.
- `take_write_lock` in autocommit releases the lock at once, so the duplicate lookup runs
  unlocked.
- Corroboration checks only the newest row, so a document linked to an older row is linked twice
  and `corroboration_added` stays true.

Fetcher, in `tests/sources/test_open_issues.py`:

- Connect timeout times address count overruns the request cap and budget.
- A literal space is not normalised to `%20` in robots matching.
- Every host builds its own SSL context.

Adapter CLI, in `tests/db/test_open_issues_cli.py`:

- A bad `FETCH_IPV6` is read after the run starts and leaves a `running` job row.

Flare, in `tests/db/test_flare_alert.py`:

- An alert names a unit by code, not by tag.

## Data credits

- News feeds: Hydrocarbon Processing, read for reference use
  ([035](docs/decisions/035-hp-reading-is-not-ai-training.md)).
- We acknowledge the use of data and imagery from NASA's Fire Information for Resource Management
  System (FIRMS), part of NASA's Earth Science Data and Information System (ESDIS). VIIRS 375 m active
  fire products VNP14IMGT (S-NPP) and VJ114IMGT (NOAA-20).
- © OpenStreetMap contributors, https://www.openstreetmap.org/copyright. Data via the Overpass
  API.
- GDELT Project, https://www.gdeltproject.org.
- U.S. Energy Information Administration (EIA).
