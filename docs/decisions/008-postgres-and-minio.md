# 008: Postgres and MinIO on the data Droplet

Date: 2026-11-11

Status: amended 2026-11-12. Postgres stands. The object store is Cloudflare R2; see [029](029-cloudflare-r2-object-store.md).

## Context

The system needs one relational database for events, prices and job runs, and a store for raw
fetched documents, keyed by content hash. It runs unattended on three small Droplets. The data
Droplet has 1 GB of RAM and is already paid for.

## Options

- Postgres 16 and MinIO in Compose on the data Droplet. No new monthly cost and one S3 API for
  raw documents and, later, dumps. The cost is that backups, upgrades and memory limits are ours,
  and both services share 1 GB.
- DigitalOcean Managed Postgres. Daily backups, point-in-time restore and minor upgrades come
  with it. The cost is a monthly fee higher than the data Droplet, a small connection limit on the
  smallest size, and a second account surface to secure.
- DigitalOcean Spaces for raw documents. Durable and off the box. The cost is a flat monthly fee,
  another set of access keys, and documents leaving the VPC on every write.
- Raw documents as `bytea` in Postgres, no object store. One service fewer. The cost is a database
  that grows with every fetched page, slower dumps and restores, and no bucket for later dumps.

## Decision

Postgres 16 and MinIO in Compose on the data Droplet, bound to its VPC address. Postgres keeps
rows. MinIO keeps raw documents in a bucket named `raw`, at `{source}/{yyyy}/{mm}/{dd}/{sha}.html`.
At this size the managed services buy durability we can get from a nightly dump and an off-box
copy, for more than the whole stack costs today.

## Consequences

Memory is tight and fixed: Postgres 384 MB, MinIO 256 MB, a one-shot bucket job 64 MB, which
leaves about 300 MB for the OS and Docker. Until the backup job exists, all data sits on one
disk. Image updates are a manual pin bump.

To undo: Postgres moves with `pg_dump` and `pg_restore`. Raw documents move with `mc mirror` to
any S3 store, and the code changes only its endpoint and keys.

## Amendment, 2026-11-11

When the stack was built, no MinIO server image could be pulled from a publisher we trust. Docker
Hub has no `minio/minio` or `minio/mc` repository, quay.io and ghcr.io refuse anonymous pulls, and
Bitnami has no image. The one image that resolves, `chainguard/minio`, comes from a different
publisher. Postgres is built as decided, at 384 MB. The object store is held, with 320 MB of the
data Droplet (256 MB for the service, 64 MB for a setup job) kept free for it, until the owner
chooses between MinIO from another image, Garage, SeaweedFS and DigitalOcean Spaces. That choice
gets its own record, which replaces the MinIO part of this one.
