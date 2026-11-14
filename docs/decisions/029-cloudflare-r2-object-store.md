# 029: Cloudflare R2 for raw documents and backups

Date: 2026-11-12

Replaces the MinIO part of [008](008-postgres-and-minio.md).

## Context

Record 008 put MinIO on the data Droplet. Its amendment says no MinIO image could be confirmed
from a publisher we trust. The nightly backup also needs a copy off the box, which no store on the
data Droplet can give.

## Options

- Cloudflare R2. S3 API, no egress fee, off DigitalOcean. The cost is one more account and
  three tokens to manage.
- DigitalOcean Spaces. S3 API, off the box. The cost is a flat monthly fee, and the copy sits in
  the same account as the Droplets, so one leaked token reaches both.
- Garage on the data Droplet. Small and has an official image. The cost is 320 MB of a 1 GB box,
  and no off-box copy.
- MinIO built from source. Keeps the 008 design. The cost is building, scanning and rebuilding
  the image for every upstream fix, and still no off-box copy.

## Decision

R2, for raw documents and for backups, with backups in a separate bucket. One endpoint serves both
buckets. Code uses the S3 API and reads `OBJECT_STORE_ENDPOINT`, `OBJECT_STORE_REGION` (`auto`),
`OBJECT_STORE_BUCKET_RAW`, `OBJECT_STORE_BUCKET_BACKUP`, `OBJECT_STORE_ACCESS_KEY`,
`OBJECT_STORE_SECRET_KEY`, `OBJECT_STORE_BACKUP_ACCESS_KEY` and `OBJECT_STORE_BACKUP_SECRET_KEY`.
Tests mock S3. No object store container runs on the data Droplet, so the 320 MB that 008 held is
released.

Raw documents go in the bucket named by `OBJECT_STORE_BUCKET_RAW`, at
`raw/{source}/{yyyy}/{mm}/{dd}/{sha}.{ext}`. The `ext` comes from the stored body type, `html` for
article pages and `json` for API responses, as the adapters write it.

There are three tokens, each scoped to one bucket:

- Ingest holds the raw token, Object Read & Write on the raw bucket.
- App holds a read-only raw token, Object Read on the raw bucket. It uses the same
  `OBJECT_STORE_ACCESS_KEY` and `OBJECT_STORE_SECRET_KEY` names, with a different value per Droplet.
- The data Droplet holds only the backup token, Object Read & Write on the backup bucket, in
  `OBJECT_STORE_BACKUP_ACCESS_KEY` and `OBJECT_STORE_BACKUP_SECRET_KEY`.

The laptop holds no backup key, and `OBJECT_STORE_BACKUP_*` exists only in the data Droplet's
`.env`.

R2 has no object lock, so write access is also delete access. A break-in on the data Droplet can
delete every backup. A break-in on ingest or app cannot reach the backups. This replaces the
write-only backup key in record 033.

## Consequences

Raw documents and backups leave DigitalOcean over TLS. R2 bills per GB stored and per operation,
with no egress fee. The backup copy is off the box and outside the DigitalOcean account. We add one
account and three tokens to rotate.

To undo: copy the buckets to any S3 store and change the endpoint and keys. No code changes.
