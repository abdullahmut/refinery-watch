# 035: Reading Hydrocarbon Processing is reference use, not AI training

Date: 2026-11-12

## Context

The Hydrocarbon Processing robots terms allow reference use and disallow AI training. Our HP
adapter fetches articles, and the extraction step sends their text to a language model API. We
must say plainly which side of that line this falls on before the source runs.

## Options

- Treat any model call as AI training and drop HP. Safe, but we lose a main refining news source.
- Treat extraction as reference use and run HP, with a clear stop rule.

## Decision

We run HP as reference use. We read an article to pull outage facts (plant, unit, dates, cause)
into our own database for a refined products desk. Exactly this happens: the article is fetched,
stored in our object store, and its text is sent to the Anthropic API for extraction only. No
model is trained or tuned on the text, by us or for us. We rely on the provider's commercial
terms, which say API inputs are not used to train its models. We have not audited the provider.

Two documents decide this: the Hydrocarbon Processing site terms and robots.txt, and Anthropic's
commercial terms of service. Neither document has been re-read on a recorded date; the operator
reads both and records the date here before the HP adapter runs unattended. Nothing in the code
watches either for changes; the operator re-reads them at each review of this record.

Record 015 already sets the fetch rules and the live robots.txt check. This record does not change
them. If the publisher objects, or either document changes against us, we stop. The edit: set
`robots_status` to `withdrawn` for `hp_news` and `hp_refining` in data/sources.csv, update
`SOURCES_CSV_SHA256` in tests/test_sources_csv.py, and reload the source list. `withdrawn` is
outside the cleared set, and a test checks that the gate refuses it. We then ask the publisher
what it wants done with the copies already stored.

## Consequences

HP can run under the cleared status it already has. The clearance rests on two things we do not
control: the publisher's terms and the provider's no-training terms. If either changes, HP stops
until we decide again. To undo, change the two status values and the hash. No other code changes.
