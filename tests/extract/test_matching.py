from refinery_watch.extract.matching import (
    RefineryHit,
    RefineryMatcher,
    RefineryRef,
    match_form,
)


def ref(rid: str, name: str, *aliases: str) -> RefineryRef:
    return RefineryRef(
        id=rid,
        name=name,
        aliases=aliases,
        country="Testland",
        owner=None,
        nameplate_kbd=None,
        status="operating",
    )


PORT_ARTHUR = [
    ref("R9101", "Alpha Port Arthur Refinery", "Port Arthur"),
    ref("R9102", "Gamma Works", "Beta Port Arthur"),
]
CHIBA = [
    ref("R9111", "Eneos Chiba Refinery"),
    ref("R9112", "Cosmo Chiba Refinery"),
    ref("R9113", "Keiyo Plant", "Chiba"),
]


def scores(matcher: RefineryMatcher, text: str) -> dict[str, int]:
    return {h.refinery_id: h.score for h in matcher.find(text)}


def test_match_form_strips_accents_case_and_punctuation() -> None:
    assert match_form("  Cardón,  PORT-Arthur!! ") == "cardon port arthur"
    assert match_form("") == ""
    assert match_form("Straße") == "strasse"


def test_split_name_scores_95_and_the_alias_phrase_100() -> None:
    matcher = RefineryMatcher(PORT_ARTHUR)
    got = scores(matcher, "Beta said its Port Arthur refinery was shut.")
    assert got == {"R9101": 100, "R9102": 95}
    first = matcher.find("Beta said its Port Arthur refinery was shut.")[0]
    assert first == RefineryHit("R9101", 100, "Port Arthur")


def test_same_city_collisions() -> None:
    matcher = RefineryMatcher(CHIBA)
    assert scores(matcher, "Eneos Chiba restarted its unit.") == {"R9111": 100, "R9113": 85}
    assert scores(matcher, "The Chiba refinery was shut.") == {"R9113": 85}


def test_name_words_must_share_a_sentence_for_95() -> None:
    matcher = RefineryMatcher([ref("R9120", "Alpha Beta Refinery")])
    assert scores(matcher, "Beta unit. Alpha said so.") == {}
    assert scores(matcher, "Beta unit was shut by Alpha.") == {"R9120": 95}
    assert scores(matcher, "Alpha Beta unit.") == {"R9120": 100}


def test_accents_and_case_do_not_change_a_score() -> None:
    matcher = RefineryMatcher([ref("R9130", "Cardon Refinery", "Cardon")])
    assert scores(matcher, "CARDÓN shut") == scores(matcher, "cardon shut") == {"R9130": 100}


def test_accented_alias_matches_plain_text() -> None:
    matcher = RefineryMatcher([ref("R9131", "Plain Works", "Cardón")])
    assert scores(matcher, "cardon was shut") == {"R9131": 85}


def test_the_word_refinery_in_a_name_is_ignored() -> None:
    matcher = RefineryMatcher([ref("R9140", "Testville Refinery")])
    assert scores(matcher, "Testville was shut") == {"R9140": 100}
    assert scores(matcher, "the refinery was shut") == {}


def test_whole_words_only() -> None:
    matcher = RefineryMatcher([ref("R9150", "Zed Works", "ZPC")])
    assert scores(matcher, "ZPCX unit") == {}
    assert scores(matcher, "the ZPC unit") == {"R9150": 85}


def test_more_than_25_hits_returns_25_best_first() -> None:
    refs = [ref(f"R{9200 + n}", f"Plant{n} Works", "Shared") for n in range(30)]
    matcher = RefineryMatcher([*refs, ref("R9199", "Shared Works")])
    hits = matcher.find("Shared works were shut")
    assert len(hits) == 25
    assert hits[0].refinery_id == "R9199"
    assert hits[0].score == 100
    assert all(h.score == 85 for h in hits[1:])
    assert [h.refinery_id for h in hits[1:]] == sorted(h.refinery_id for h in hits[1:])
    assert len(matcher.find("Shared works were shut", limit=3)) == 3


def test_find_name_returns_the_one_alias_owner() -> None:
    matcher = RefineryMatcher([ref("R9160", "Mariner Works", "Marifu"), ref("R9161", "Other")])
    assert matcher.find_name("Marifu") == [RefineryHit("R9160", 85, "Marifu")]
    assert matcher.find_name("nothing here") == []


def test_empty_names_and_text() -> None:
    matcher = RefineryMatcher([ref("R9170", "Refinery", "The")])
    assert matcher.find("the refinery") == []
    assert RefineryMatcher([]).find("anything") == []
