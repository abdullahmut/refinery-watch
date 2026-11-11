import subprocess
import sys

import pytest

from refinery_watch.extract.keywords import EVENT_TERMS
from refinery_watch.extract.matching import RefineryRef, match_form
from refinery_watch.extract.prefilter import build_term_matcher


def ref(rid: str, name: str, *aliases: str) -> RefineryRef:
    return RefineryRef(rid, name, aliases, "Testland", None, None, "operating")


REFINERIES = [
    ref("R9101", "Alpha Port Arthur Refinery", "Port Arthur"),
    ref("R9130", "Cardon Refinery", "Cardon"),
]


def test_importing_the_prefilter_loads_no_model_or_seed_code() -> None:
    code = (
        "import sys, refinery_watch.extract.prefilter\n"
        "banned = ('anthropic', 'pydantic', 'refinery_watch.llm', 'refinery_watch.db.seed',"
        " 'psycopg')\n"
        "print([m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in banned)])\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_a_matcher_is_one_string_in_a_sequence_out() -> None:
    matcher = build_term_matcher(REFINERIES)
    result = matcher("anything")
    assert not isinstance(result, str)
    assert list(result) == []


def test_event_word_and_refinery_pass() -> None:
    matcher = build_term_matcher(REFINERIES)
    assert matcher("Valero's Port Arthur refinery was shut.") == ("shut", "refinery:R9101")


def test_only_an_event_word_fails() -> None:
    assert build_term_matcher(REFINERIES)("A plant was shut yesterday.") == ()


def test_only_a_refinery_name_fails() -> None:
    assert build_term_matcher(REFINERIES)("Port Arthur is a city in Texas.") == ()


def test_event_words_match_whole_words_only() -> None:
    assert build_term_matcher(REFINERIES)("Shutterstock photo of Port Arthur") == ()


def test_accented_text_matches_plain_alias() -> None:
    assert build_term_matcher(REFINERIES)("Fire at the Card\u00f3n refinery") == (
        "fire",
        "refinery:R9130",
    )


def test_phrases_and_several_refineries() -> None:
    text = "Port Arthur taken offline after a power outage; Cardon restarted."
    assert build_term_matcher(REFINERIES)(text) == (
        "outage",
        "offline",
        "taken offline",
        "restarted",
        "power outage",
        "refinery:R9101",
        "refinery:R9130",
    )


def test_event_terms_are_unique_and_already_in_match_form() -> None:
    assert len(set(EVENT_TERMS)) == len(EVENT_TERMS)
    for term in EVENT_TERMS:
        assert match_form(term) == term


@pytest.mark.parametrize("term", ["shut", "ramp up", "force majeure", "back online"])
def test_expected_terms_present(term: str) -> None:
    assert term in EVENT_TERMS
