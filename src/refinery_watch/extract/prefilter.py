"""Keyword pre-filter. A text passes when it has an event word and names a refinery."""

from collections.abc import Callable, Sequence

from refinery_watch.extract.keywords import EVENT_TERMS, match_form
from refinery_watch.extract.matching import RefineryMatcher, RefineryRef

TermMatcher = Callable[[str], Sequence[str]]


def build_term_matcher(refineries: Sequence[RefineryRef]) -> TermMatcher:
    """Returns event terms then refinery:<id> entries, or nothing unless both kinds are found."""
    matcher = RefineryMatcher(refineries)
    padded_terms = [(term, f" {term} ") for term in EVENT_TERMS]

    def terms_in(text: str) -> tuple[str, ...]:
        padded = f" {match_form(text)} "
        events = [term for term, needle in padded_terms if needle in padded]
        if not events:
            return ()
        hits = matcher.find(text)
        if not hits:
            return ()
        return (*events, *(f"refinery:{h.refinery_id}" for h in hits))

    return terms_in
