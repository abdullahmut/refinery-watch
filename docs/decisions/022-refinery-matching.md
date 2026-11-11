# 022: Matching refinery names in article text

Date: 2026-11-11

## Context

The model may only name refineries from a candidate list built from the article. The golden set
has same-city collisions: two refineries at Port Arthur, three at Chiba, two at Dalian. Articles
split names ("Valero's Port Arthur refinery"), drop accents ("Cardon"), and use acronyms (ZPC,
MRPL). The confidence rules need a match score. The adapters also need the same matching when they
insert a document, because the pre-filter result is stored on the row and cannot be changed later.

## Options

- A fuzzy-matching library. Finds near misses, adds a dependency and scores that are hard to
  explain.
- Exact alias match only. Misses split names, so Valero Port Arthur never becomes a candidate.
- Exact phrase match, then a simple normalised match: every word of a name or alias in one
  sentence.

## Decision

The third, in plain Python. Text and names are normalised: accents stripped, case folded,
punctuation turned to spaces. "refinery", "the" and "of" are dropped from names. A refinery scores:

- 100 when its name or a multi-word alias appears as a phrase;
- 95 when every word of its name or a multi-word alias appears in one sentence;
- 85 when only a one-word alias appears, such as a city name.

Every refinery that scores goes to the model as a candidate, up to 25, best first. The model picks.
The stored `match_score` is the best score of the refinery it picked, so a pick made only on a city
name stays below the 90 that `medium` confidence needs. A name the model reports as unmatched goes
to review, with a suggested id if the same rules find exactly one refinery.

The matcher and the keyword pre-filter live in modules that import no model client and no seed
loader, so the adapters use the same code at insert time. A document passes the pre-filter when it
names a refinery by these rules and contains an event word.

## Consequences

Every score can be explained by pointing at the words. Spelling errors and transliterations are
missed and show up in review as alias gaps, which the operator fixes in the reference file. A
change to the rules or the keyword list applies only to documents stored after it. To undo,
replace one module.
