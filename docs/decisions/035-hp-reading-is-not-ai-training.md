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
into our own database for a refined products desk. That is the same use as an analyst reading it.
No model is trained or tuned on the text, by us or for us. The text goes to the model provider's
API only for extraction. We rely on the provider's commercial API terms, which say API inputs are
not used to train its models. We have not audited the provider. We take that commitment at its
word and re-read the terms when they change.

Record 015 already sets the fetch rules and the live robots.txt check. This record does not change
them. If the publisher objects, or its terms change, we stop. The operator sets the `robots_status`
of `hp_news` and `hp_refining` to a value outside the cleared set and reloads the source list, and
the gate refuses both rows on the next run. We then ask the publisher what it wants done with the
copies already stored.

## Consequences

HP can run under the cleared status it already has. The clearance rests on two things we do not
control: the publisher's terms and the provider's no-training terms. If either changes, HP stops
until we decide again. To undo, change the two status values. No code changes.
