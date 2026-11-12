# 043: Social media is not a source in v1

Date: 2026-11-12

## Context

We were asked whether X, Instagram, Bluesky and Reddit should be sources. Posts about plant
upsets may appear there before the news, but we have no evidence of that. None of the four is in
data/sources.csv.

## Options

Every access fact below is to confirm on the date of use.

- X. Terms: X's own developer terms govern API use. Scraping the site would also run against
  record 015, which forbids evading a block. Those are two separate problems. Cost: reading posts
  by API is believed to need a paid tier; tiers and prices to confirm.
- Instagram. Terms: believed to bar automated scraping; to confirm. Access: whether the Graph API
  allows search across public posts is to confirm.
- Bluesky. Access: believed to offer a free public API. Terms and rate limits to confirm.
- Reddit. Access: believed to offer a free tier for small use, possibly needing approval. The
  tier, approval, terms and rate limits are to confirm.
- None of them. We may lose some early hints, and the source list stays news, company and
  regulator feeds.

## Decision

v1 uses none of them, for three reasons. Noise: we expect most posts that name a refinery not to
be about an outage. This is a forecast, but if true the keyword pre-filter would pass many of
them to the model and eat into the daily spend cap. Attribution: a post rarely says who saw what
and when, so it cannot back a unit, a date or a capacity figure, and every note line needs a
source a desk can check. Terms and cost: each platform's terms, and for X the cost of access,
must be checked before any use, and none has been.

Bluesky and Reddit are worth a later trial, as weak signals only. A post would never create an
`outage_event` row, never count as corroboration, never raise confidence and never count as
capacity. Before any code, three steps, in order. First, a decision record that changes
definitions.md to add a social-signal class. Second, the operator reads the terms and rate limits,
records the date, and a status for the source is added to `CLEAR_STATUSES`. Third, the source row
goes into data/sources.csv and `SOURCES_CSV_SHA256` in tests/test_sources_csv.py is updated.

## Consequences

The source list stays small and every note line has a checkable source. If posts do come first,
we hear of some upsets later than a desk that watches them. No code depends on this choice. To
change it, follow the trial steps above.
