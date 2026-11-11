from datetime import date

import pytest

from refinery_watch.dates import Tense, resolve_weekday

THU = date(2026, 11, 12)


def test_repro_cases() -> None:
    assert resolve_weekday(THU, 2, "future") == date(2026, 11, 18)
    assert resolve_weekday(THU, 1, "past") == date(2026, 11, 10)
    assert resolve_weekday(THU, 3, "last") == date(2026, 11, 5)
    assert resolve_weekday(THU, 3, "next") == date(2026, 11, 19)


def test_same_weekday_for_past_and_future_is_the_publication_date() -> None:
    assert resolve_weekday(THU, 3, "past") == THU
    assert resolve_weekday(THU, 3, "future") == THU


@pytest.mark.parametrize("tense", ["past", "last"])
def test_backward_tenses_never_go_forward(tense: Tense) -> None:
    for weekday in range(7):
        assert resolve_weekday(THU, weekday, tense) <= THU


@pytest.mark.parametrize("tense", ["future", "next"])
def test_forward_tenses_never_go_back(tense: Tense) -> None:
    for weekday in range(7):
        assert resolve_weekday(THU, weekday, tense) >= THU


def test_result_has_the_requested_weekday() -> None:
    for weekday in range(7):
        for tense in ("past", "future", "last", "next"):
            assert resolve_weekday(THU, weekday, tense).weekday() == weekday


def test_weekday_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="0 to 6"):
        resolve_weekday(THU, 7, "past")
    with pytest.raises(ValueError, match="0 to 6"):
        resolve_weekday(THU, -1, "past")


def test_unclear_tense_raises() -> None:
    with pytest.raises(ValueError, match="tense"):
        resolve_weekday(THU, 1, "unclear")  # type: ignore[arg-type]
