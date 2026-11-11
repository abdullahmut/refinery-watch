# 018: The content hash covers normalised text, not page bytes

Date: 2026-11-11

## Context

The project rule is to hash normalised content with SHA-256 and never process the same hash twice.
"Normalised" was not defined. The same article page changes from one fetch to the next: ad slots,
session tokens and related-story boxes. One story also appears in two Hydrocarbon Processing feeds.

## Options

- Hash the raw bytes. Simple, but nearly every refetch is "new", so the same story reaches the
  model again and costs money.
- Hash the extracted headline and body text after normalising it: Unicode NFC, runs of whitespace
  collapsed, blank lines removed. The same story hashes the same way unless its words change.
- Also skip any URL already stored, before fetching it.

## Decision

The hash is SHA-256 of the UTF-8 bytes of the normalised headline and body text. For EIA it is the
data rows as canonical JSON, without the request echo. The original bytes still go to the object
store, under the key `raw/{source}/{yyyy}/{mm}/{dd}/{sha}.{ext}` in the raw-documents bucket named
in config, with the date taken from the fetch time in UTC. An article whose URL is already in
`raw_document` is not fetched again.

## Consequences

Fewer duplicate model calls, and polite to publishers. A story that is edited at the same URL is
not picked up again. A correction then has to reach us through another source or another URL. A
change to the text extraction changes the hashes, so stories fetched after that change may be
stored once more. To undo, hash the bytes and drop the URL check. Stored rows keep their hashes.
