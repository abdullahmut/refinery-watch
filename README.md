# refinery-watch

## What this is

An in-house data layer that a refined products desk would build on top of its vendor data. It
keeps a point-in-time history of refinery outages, so a backtest sees only what was known on the
day, and it adds alt-data signals such as satellite flaring.

It does not replace a commercial outage service. Its limits:

- Coverage is whatever public sources report. Operators often say nothing, or say it late.
- Capacity figures are as reported. Many reports give none, and the row says so.
- It is one developer's project, not a vendor product. No history is loaded yet.

## What is built today

- An append-only outage store in Postgres, with `outage_as_of(T)` queries.
- Source adapters for the Hydrocarbon Processing RSS feeds, GDELT DOC and EIA weekly refinery
  utilisation. Each fetch checks robots.txt and a per-source status gate before it runs.
- The VIIRS flare signal, on synthetic data. The fetch code is tested against hand-made fixtures.
- Terraform and Compose for the three Droplets.

Nothing is deployed. No live data is loaded. The flare method has not been run on real FIRMS
history, so it reports no hit rate or lead time. See
[docs/research/flare-lead-time.md](docs/research/flare-lead-time.md).

## Specified but not built

- LLM extraction of events from article text.
- The morning note.
- Scheduling and monitoring.
- Deploy.

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
and backups go to Cloudflare R2 ([029](docs/decisions/029-cloudflare-r2-object-store.md)).
Terraform state is local ([006](docs/decisions/006-terraform-state-location.md)). Layout and
memory limits are in [docs/architecture.md](docs/architecture.md).

## Point-in-time rule

Every row carries `known_at`, the time the system learned it. A revision is a
new row whose `supersedes` points at the row it replaces. The old row is never marked. Current rows
are those no later row supersedes. `outage_as_of(T)` filters both sides of that test by
`known_at <= T`, so a revision written after T cannot change the answer for T. The tests cover
this ([tests/db/test_outage_as_of.py](tests/db/test_outage_as_of.py)).

Dates are stored in UTC.

## Signals against capacity

Flaring and regulator records are signals. They are never capacity. A flare cannot name a unit or
say how much was lost, so it creates no event and adds no barrels. At most it lifts a `low`
confidence row to `medium` at query time, when every other condition for `medium` already holds
([040](docs/decisions/040-flare-confidence-rule.md)). A regulator-only row stays a regulator
report.

## Running it locally

Needs GNU Make 4, uv, Docker and bash. On Windows use Git Bash.

```
make test               # unit tests, no database
make test-integration   # needs the local Postgres from make up
make lint
make up                 # Postgres on 127.0.0.1:5432
make down
```

Copy `.env.example` to `.env` before `make up`. The keys the local stack needs:
`POSTGRES_PASSWORD`, `PG_OWNER_PASSWORD` and `PG_APP_PASSWORD`. Jobs also read `PG_HOST`,
`PG_DATABASE`, `PG_APP_USER`, the `OBJECT_STORE_*` keys, `CONTACT_EMAIL` and, for the adapters that
need them, `EIA_API_KEY` and `FIRMS_MAP_KEY`. Never commit `.env`.

## Repository map

- `src/refinery_watch/db/` connection, migrations, seed loaders and event writes.
- `src/refinery_watch/sources/` fetcher, robots, and the three adapters.
- `src/refinery_watch/extract/` keyword pre-filter and refinery matching.
- `src/refinery_watch/flare/` footprints, FIRMS fetch, score, alerts.
- `src/refinery_watch/mail/` mail sender.
- `data/` source list and unit-to-product map.
- `infra/terraform/`, `infra/cloud-init/`, `infra/compose/` the three Droplets.
- `tests/` mirrors `src/`. Tests marked `integration` need Postgres.
- `docs/` architecture, definitions, runbook, research and decision records.

## Known open issues

Five tests are marked `xfail(strict=True)` on the flare branch. Each states a defect that is not
fixed yet. A strict xfail fails the run if the defect disappears, so the mark cannot go stale.

- [tests/sources/test_open_issues.py](tests/sources/test_open_issues.py): connect timeout times
  address count overruns the request cap; a literal space is not normalised in robots matching;
  every host builds its own SSL context.
- [tests/db/test_open_issues_cli.py](tests/db/test_open_issues_cli.py): a bad `FETCH_IPV6` leaves
  a `running` job row.
- [tests/db/test_flare_alert.py](tests/db/test_flare_alert.py): an alert names a unit by code, not
  by tag.

## Data credits

- Fire detections: NASA FIRMS (VIIRS active fire).
- Plant outlines: OpenStreetMap contributors, under the ODbL.
- Refinery utilisation: U.S. Energy Information Administration (EIA).
- News leads: the GDELT Project.
