import json
from datetime import date
from decimal import Decimal

import pytest
from tests.sources.helpers import sample_bytes

from refinery_watch.sources.eia_wpsr import (
    SERIES,
    EiaResponseError,
    build_params,
    normalised_content,
    parse_data,
    redact,
)
from refinery_watch.sources.store import PriceRow

KEY = "HANDMADEKEY123"


def test_handmade_rows() -> None:
    rows = parse_data(sample_bytes("eia_v2_data_handmade.json"))
    assert rows == [
        PriceRow("eia.testseries1", date(2026, 11, 6), Decimal("90.1"), "percent"),
        PriceRow("eia.testseries2", date(2026, 11, 6), Decimal("16200"), "kbd"),
        PriceRow("eia.testseries2", date(2026, 10, 30), Decimal("16150.5"), "kbd"),
    ]


def test_string_and_number_values_agree() -> None:
    def one(value: str) -> Decimal:
        body = (
            '{"response": {"data": [{"period": "2026-11-06", "series": "TESTSERIES1",'
            f' "value": {value}, "units": "percent"}}]}}}}'
        )
        return parse_data(body.encode())[0].value

    assert one('"90.1"') == one("90.1") == Decimal("90.1")


def test_null_and_other_series_are_skipped() -> None:
    rows = parse_data(sample_bytes("eia_v2_data_handmade.json"))
    assert all(r.series in SERIES.values() for r in rows)
    assert date(2026, 10, 30) not in [r.observed_on for r in rows if r.series.endswith("1")]


def test_error_file_raises_without_text_from_the_reply() -> None:
    with pytest.raises(EiaResponseError, match="code 403") as info:
        parse_data(sample_bytes("eia_v2_error_handmade.json"))
    assert "Invented" not in str(info.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"response": {}}',
        b'{"response": {"data": [{"period": "bad", "series": "TESTSERIES1", "value": "1"}]}}',
        b'{"response": {"data": [{"period": "2026-11-06", "series": "TESTSERIES1", "value": "x"}]}}',  # noqa: E501
    ],
)
def test_bad_bodies_raise(body: bytes) -> None:
    with pytest.raises(EiaResponseError):
        parse_data(body)


def test_normalised_content_ignores_order_and_request_block() -> None:
    rows = parse_data(sample_bytes("eia_v2_data_handmade.json"))
    shuffled = list(reversed(rows))
    assert normalised_content(rows) == normalised_content(shuffled)
    changed = json.loads(sample_bytes("eia_v2_data_handmade.json"))
    changed["request"]["params"]["frequency"] = "monthly"
    changed["apiVersion"] = "9.9"
    assert normalised_content(parse_data(json.dumps(changed).encode())) == normalised_content(rows)


def test_normalised_content_changes_with_a_value() -> None:
    rows = parse_data(sample_bytes("eia_v2_data_handmade.json"))
    revised = [PriceRow(r.series, r.observed_on, r.value + 1, r.unit) for r in rows[:1]] + rows[1:]
    assert normalised_content(rows) != normalised_content(revised)


def test_trailing_zeros_do_not_change_the_content() -> None:
    a = [PriceRow("eia.testseries1", date(2026, 11, 6), Decimal("90.10"), "percent")]
    b = [PriceRow("eia.testseries1", date(2026, 11, 6), Decimal("90.1"), "percent")]
    assert normalised_content(a) == normalised_content(b)


def test_redact_removes_the_key_everywhere() -> None:
    body = sample_bytes("eia_v2_data_handmade.json")
    assert KEY.encode() in body
    redacted = redact(body, KEY)
    assert KEY.encode() not in redacted
    assert b"REDACTED" in redacted
    assert redact(b"abc", "") == b"abc"


def test_build_params() -> None:
    params = build_params("K1")
    assert params[0] == ("api_key", "K1")
    assert ("frequency", "weekly") in params
    assert ("data[0]", "value") in params
    assert ("sort[0][direction]", "desc") in params
    assert ("length", "200") in params
    facets = [v for k, v in params if k == "facets[series][]"]
    assert facets == list(SERIES)


def test_boolean_values_are_skipped_not_read_as_numbers() -> None:
    body = (
        b'{"response": {"data": [{"period": "2026-11-06", "series": "TESTSERIES1",'
        b' "value": true}, {"period": "2026-11-06", "series": "TESTSERIES2", "value": 5}]}}'
    )
    assert [r.series for r in parse_data(body)] == ["eia.testseries2"]
