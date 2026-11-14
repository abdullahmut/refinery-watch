# 017: EIA weekly utilisation through API v2

Date: 2026-11-11

## Context

The note shows US refinery utilisation from EIA's weekly data. EIA publishes it two ways: an XLS
workbook on its `dnav` pages that needs no key, and API v2, which needs a free key. The source row
`eia_wpsr` already points at API v2.

## Options

- XLS workbook. No key. The cost is a reader package for the old binary `.xls` format, and a parser
  tied to header rows and column positions that EIA can change without notice. The file name we
  have has not been checked.
- API v2. JSON, read with the standard library. Each row carries its own series id, period, value
  and units. The cost is a key in config, and the key appears in the request URL.

## Decision

API v2 for production. The key comes from `EIA_API_KEY` in config. The adapter asks for a fixed
list of series ids, confirmed from the route metadata file. Each value is written to the `price`
table as series `eia.<series id in lower case>`. A value is written only when it is new or
different from the latest one known, so EIA revisions become new rows with a later `known_at`. The
key is removed from the stored response, and it is never written to a log or a `job_run` error.
No XLS reader is added.

## Consequences

There is no new dependency, and the parser does not depend on a sheet layout. If the key is lost or
revoked, the weekly figure goes missing until the operator replaces it. The XLS fixture is not
used. To switch to the XLS, add a reader package and a parser. The price rows stay the same.
