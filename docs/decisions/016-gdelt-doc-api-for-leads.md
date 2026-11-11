# 016: GDELT DOC 2.0 ArtList for English leads

Date: 2026-11-11

## Context

GDELT is a lead source. A hit is never a fact until the article itself has been fetched and read.
The source list describes the GKG bulk route: three zips every 15 minutes, with no title field,
filtered against refinery aliases. The Friday scope needs English leads only.

## Options

- GKG bulk files. These give high volume and the translated feed later. The cost is three zips
  every 15 minutes, unzip and tab-split, and alias matching of our own.
- DOC 2.0 ArtList. One small JSON call per run, already filtered by query and by language, and it
  carries the URL, title and seen time. The cost is that `seendate` is when GDELT saw the page,
  not when it was published, and the API rate-limits with a plain-text reply.

## Decision

Use DOC 2.0 ArtList with `sourcelang:english` for the first GDELT adapter. Each lead's article page
is fetched through the normal fetcher. The publication time comes from that page, never from
`seendate`. If a page states no publication time, the lead is dropped and counted. A page
published more than 14 days before the fetch is dropped too, because GDELT often re-sees old
stories. Stored articles take a new source id, `gdelt_doc`. The `gdelt_gkg` row stays as it is, for
the bulk route later.

## Consequences

The adapter is about a hundred lines and needs no unzip. Leads that have no date on the page are
lost. That is the price of never putting a wrong date on an old story. The query is fixed in code,
so a change to it shows up in review. The bulk route is still open for volume and for
non-English press. To undo, retire `gdelt_doc` and build the GKG adapter. Rows already stored keep
their source id.
