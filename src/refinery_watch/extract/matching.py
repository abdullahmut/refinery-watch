"""Refinery names in free text, scored by record 022. Standard library only."""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

MatchScore = Literal[100, 95, 85]

DROPPED_WORDS: Final = frozenset({"refinery", "the", "of"})
DEFAULT_LIMIT: Final = 25
_NOT_WORD = re.compile(r"[\W_]+")
_SENTENCE_END = re.compile(r"[.!?\n]+")


def match_form(text: str) -> str:
    """Accents removed, case folded, every run of non-letters and non-digits one space."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NOT_WORD.sub(" ", stripped.casefold()).strip()


@dataclass(frozen=True, slots=True)
class UnitRef:
    id: int
    unit_tag: str
    capacity_kbd: Decimal | None


@dataclass(frozen=True, slots=True)
class RefineryRef:
    id: str
    name: str
    aliases: tuple[str, ...]
    country: str
    owner: str | None
    nameplate_kbd: Decimal | None
    status: Literal["operating", "closed_or_converted"]
    units: Mapping[str, tuple[UnitRef, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefineryHit:
    refinery_id: str
    score: MatchScore
    matched: str


@dataclass(frozen=True, slots=True)
class _Name:
    raw: str
    words: tuple[str, ...]
    is_name: bool

    @property
    def phrase(self) -> str:
        return " " + " ".join(self.words) + " "


def _words(text: str) -> tuple[str, ...]:
    return tuple(w for w in match_form(text).split() if w not in DROPPED_WORDS)


class RefineryMatcher:
    def __init__(self, refineries: Sequence[RefineryRef]) -> None:
        self._entries: list[tuple[str, list[_Name]]] = []
        for ref in refineries:
            names: list[_Name] = []
            for raw, is_name in [(ref.name, True), *((a, False) for a in ref.aliases)]:
                words = _words(raw)
                if words:
                    names.append(_Name(raw, words, is_name))
            self._entries.append((ref.id, names))

    def find(self, text: str, limit: int = DEFAULT_LIMIT) -> list[RefineryHit]:
        padded = " " + match_form(text) + " "
        sentences = [set(match_form(s).split()) for s in _SENTENCE_END.split(text)]
        text_words = set(padded.split())
        hits: list[RefineryHit] = []
        for refinery_id, names in self._entries:
            best = self._best(names, padded, sentences, text_words)
            if best is not None:
                hits.append(RefineryHit(refinery_id, best[0], best[1]))
        hits.sort(key=lambda h: (-h.score, h.refinery_id))
        return hits[:limit]

    def find_name(self, name: str) -> list[RefineryHit]:
        """Matches a short name the model reported, to suggest a refinery id for review."""
        return self.find(name)

    @staticmethod
    def _best(
        names: list[_Name], padded: str, sentences: list[set[str]], text_words: set[str]
    ) -> tuple[MatchScore, str] | None:
        best: tuple[MatchScore, str] | None = None
        for item in names:
            multi = len(item.words) > 1
            score: MatchScore | None = None
            if (item.is_name or multi) and item.phrase in padded:
                score = 100
            elif (item.is_name or multi) and any(set(item.words) <= s for s in sentences):
                score = 95
            elif not item.is_name and not multi and item.words[0] in text_words:
                score = 85
            if score is not None and (best is None or score > best[0]):
                best = (score, item.raw)
        return best
