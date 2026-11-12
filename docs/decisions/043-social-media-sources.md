# 043: Social media is not a source in v1

Date: 2026-11-12

## Context

Plant upsets often show up on social media first: a photo of a flare, a post from a nearby
resident, a local reporter's first line. We were asked whether X, Instagram, Bluesky and Reddit
should be sources. Each has its own access rules, and none is in data/sources.csv today.

## Options

- X. Reading posts by API is a paid tier. The price and the read limits change often and are to
  confirm on the date of use. Scraping the site instead would break the rule against evading a
  block.
- Instagram. The terms bar automated scraping, and the official API is not meant for search
  across public posts. Not usable without breaking the terms.
- Bluesky. A free public API. Its terms and rate limits are to confirm on the date of use.
- Reddit. A free API tier for small use. Its terms, rate limits and any rules on commercial use
  are to confirm on the date of use.
- None of them. We lose some early hints, and keep the source list to news, company and
  regulator feeds.

## Decision

v1 uses none of them. Three reasons. Noise: most posts that name a refinery are not about an
outage, and the keyword pre-filter would pass a lot of them to the model and use up the daily
spend cap. Attribution: a post rarely says who saw what and when, so it cannot back a unit, a
date or a capacity figure, and the note must cite a source a desk can check. Terms: X costs
money and Instagram bars scraping, and we do not work around either.

Bluesky and Reddit are worth a later trial, as weak signals only. A post would never create an
`outage_event` row, never count as corroboration, never raise confidence and never count as
capacity. At most it points us to look for a news or regulator source. Before a trial, the
operator reads the terms and the rate limits, records the date, and adds the source to
data/sources.csv with a stated allow. The trial gets its own spec and decision record.

## Consequences

The source list stays small and every line in the note has a checkable source. We will
sometimes hear of an upset later than a desk that watches social media. To add Bluesky or
Reddit later, write the trial record and the spec. No code depends on this choice.
