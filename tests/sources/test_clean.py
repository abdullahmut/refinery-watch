import pytest

from refinery_watch.sources.clean import clean_text
from refinery_watch.sources.gdelt_doc import GdeltResponseError, parse_artlist


def test_control_characters_and_line_breaks_become_a_space() -> None:
    assert clean_text("a\nb\r\nc\x1b[31md\x00e\u2028f\x85g") == "a b c [31md e f g"


def test_plain_text_is_unchanged() -> None:
    assert clean_text("Rate limit: one every 5 seconds") == "Rate limit: one every 5 seconds"


def test_gdelt_error_text_has_no_control_characters() -> None:
    with pytest.raises(GdeltResponseError) as info:
        parse_artlist(b"line one\nFAKE LOG LINE\x1b[2J")
    assert "\n" not in str(info.value)
    assert "\x1b" not in str(info.value)
    assert "line one FAKE LOG LINE" in str(info.value)
