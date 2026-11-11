"""Known defects, recorded as strict expected failures. A fix turns each into a failure."""

import ssl
import typing

import httpx
import pytest
from tests.sources.helpers import EMAIL, Clock, make_fetcher

from refinery_watch.sources.http import (
    REQUEST_TOTAL_S,
    TIMEOUT,
    Fetcher,
    FetchError,
    PinnedTransport,
)
from refinery_watch.sources.robots import parse_robots

V4 = "93.184.216.34"


@pytest.mark.xfail(
    strict=True,
    reason="open issue: connect timeout times address count overruns the request cap and budget",
)
def test_one_attempt_is_bounded_whatever_the_address_count() -> None:
    clock = Clock()
    addresses = [f"93.184.216.{n}" for n in range(10, 20)]

    def blackhole(request: httpx.Request) -> httpx.Response:
        clock.t += TIMEOUT.connect or 0.0
        raise httpx.ConnectTimeout("timed out")

    fetcher, _, _ = make_fetcher(blackhole, clock=clock, addresses={"a.example.com": addresses})
    fetcher.set_deadline(clock.t + 100)
    start = clock.t
    with pytest.raises(FetchError):
        fetcher.get("https://a.example.com/x")
    assert clock.t - start <= 100 + REQUEST_TOTAL_S, clock.t - start


@pytest.mark.xfail(
    strict=True,
    reason="open issue: a literal space is not normalised to %20 in robots matching",
)
def test_space_and_percent20_are_the_same_octet() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /a%20b\n", "refinery-watch")
    assert not rules.allows("/a b")


@pytest.mark.xfail(
    strict=True,
    reason="open issue: every host builds its own SSL context",
)
def test_hosts_share_one_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx._transports.default as default

    built: list[int] = []
    real = default.create_ssl_context  # type: ignore[attr-defined]

    def counting(*a: typing.Any, **k: typing.Any) -> ssl.SSLContext:
        built.append(1)
        return real(*a, **k)

    monkeypatch.setattr(default, "create_ssl_context", counting)
    fetcher = Fetcher(EMAIL, resolve=lambda host: [V4], sleep=lambda s: None)
    pinned = typing.cast(PinnedTransport, fetcher._client._transport)
    for n in range(5):
        pinned.transport_for(f"h{n}.example.com")
    assert len(built) <= 1, len(built)
