from datetime import timedelta

import httpx
import pytest
from tests.sources.helpers import EMAIL, T0, Clock, Handler, make_fetcher

from refinery_watch.config import ConfigError, contact_email
from refinery_watch.sources.http import (
    TIMEOUT,
    AddressRefused,
    Fetcher,
    FetchError,
    HostBlocked,
    RobotsDisallowed,
    strip_tracking,
    user_agent,
)

OK = httpx.Response(200, content=b"hello", headers={"content-type": "text/html; charset=UTF-8"})


def test_user_agent_text() -> None:
    assert user_agent(EMAIL) == "refinery-watch/0.1 (+mailto:contact@example.com)"


def test_every_request_carries_the_user_agent() -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK)
    fetcher.get("https://a.example.com/x", accept="text/html")
    assert net.requests[0].headers["user-agent"] == (
        "refinery-watch/0.1 (+mailto:contact@example.com)"
    )
    assert net.requests[0].headers["accept"] == "text/html"


def test_contact_email_unset_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    with pytest.raises(ConfigError, match="CONTACT_EMAIL"):
        contact_email()


def test_client_timeouts() -> None:
    assert (TIMEOUT.connect, TIMEOUT.read, TIMEOUT.write, TIMEOUT.pool) == (20, 20, 20, 5)


def test_result_fields() -> None:
    fetcher, _, _ = make_fetcher(lambda r: OK)
    result = fetcher.get("https://a.example.com/x#frag")
    assert result.url == "https://a.example.com/x"
    assert result.status == 200
    assert result.content_type == "text/html"
    assert result.charset == "utf-8"
    assert result.body == b"hello"
    assert result.fetched_at == T0 + timedelta(seconds=5)


def test_params_are_sent_but_not_returned_in_url() -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK)
    result = fetcher.get("https://a.example.com/x", params=[("api_key", "SECRET123")])
    assert "api_key=SECRET123" in net.urls()[0]
    assert "SECRET123" not in result.url


def test_not_found_is_a_fetch_error_with_status() -> None:
    fetcher, net, _ = make_fetcher(lambda r: httpx.Response(404))
    with pytest.raises(FetchError) as info:
        fetcher.get("https://a.example.com/gone")
    assert info.value.status == 404
    assert len(net.requests) == 1


@pytest.mark.parametrize("url", ["ftp://a.example.com/x", "https://a.example.com:9000/x", "x"])
def test_scheme_and_port_refused_before_any_request(url: str) -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK)
    with pytest.raises(AddressRefused):
        fetcher.get(url)
    assert net.requests == []


def test_userinfo_refused() -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK)
    with pytest.raises(AddressRefused):
        fetcher.get("https://user:pw@a.example.com/x")
    assert net.requests == []


@pytest.mark.parametrize(
    "address",
    ["10.110.0.5", "127.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1", "224.0.0.1"],
)
def test_redirect_to_non_public_address_refused(address: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example.com":
            return httpx.Response(302, headers={"location": "https://inner.example.org/x"})
        return OK

    fetcher, net, _ = make_fetcher(handler, addresses={"inner.example.org": [address]})
    with pytest.raises(AddressRefused):
        fetcher.get("https://a.example.com/x")
    assert all(r.url.host != "inner.example.org" for r in net.requests)


def test_one_private_address_among_public_ones_refuses() -> None:
    fetcher, net, _ = make_fetcher(
        lambda r: OK, addresses={"a.example.com": ["93.184.216.34", "10.0.0.1"]}
    )
    with pytest.raises(AddressRefused):
        fetcher.get("https://a.example.com/x")
    assert net.requests == []


def test_resolution_failure_is_refused() -> None:
    def resolve(host: str) -> list[str]:
        raise OSError("no such host")

    fetcher = Fetcher(EMAIL, transport=httpx.MockTransport(lambda r: OK), resolve=resolve)
    with pytest.raises(AddressRefused):
        fetcher.get("https://a.example.com/x")


def test_redirect_is_followed_and_final_url_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new#top"})
        return OK

    fetcher, _, _ = make_fetcher(handler)
    assert fetcher.get("https://a.example.com/old").url == "https://a.example.com/new"


def test_sixth_redirect_refused() -> None:
    fetcher, net, _ = make_fetcher(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(FetchError, match="too_many_redirects"):
        fetcher.get("https://a.example.com/start")
    assert len(net.requests) == 6


def test_five_redirects_are_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        if n < 5:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return OK

    fetcher, _, _ = make_fetcher(handler)
    assert fetcher.get("https://a.example.com/0").body == b"hello"


def test_body_over_limit_refused() -> None:
    big = httpx.Response(200, content=b"x" * 5_000_001)
    fetcher, _, _ = make_fetcher(lambda r: big)
    with pytest.raises(FetchError, match="too_large"):
        fetcher.get("https://a.example.com/big")


def test_body_at_limit_accepted() -> None:
    fetcher, _, _ = make_fetcher(lambda r: httpx.Response(200, content=b"x" * 5_000_000))
    assert len(fetcher.get("https://a.example.com/big").body) == 5_000_000


def test_error_text_hides_query_and_chain() -> None:
    fetcher, _, _ = make_fetcher(lambda r: httpx.Response(500))
    with pytest.raises(FetchError) as info:
        fetcher.get("https://api.example.com/v2/data", params=[("api_key", "SECRET123")])
    exc = info.value
    assert "SECRET123" not in str(exc) + repr(exc)
    assert "?" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_timeout_error_has_no_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom https://x.example.com/?api_key=SECRET123")

    fetcher, _, _ = make_fetcher(handler)
    with pytest.raises(FetchError) as info:
        fetcher.get("https://x.example.com/p", params=[("api_key", "SECRET123")])
    assert "SECRET123" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_strip_tracking() -> None:
    assert (
        strip_tracking("https://a.example.com/p?id=3&utm_source=x&UTM_medium=y#frag")
        == "https://a.example.com/p?id=3"
    )
    assert strip_tracking("https://a.example.com/p?utm_a=1") == "https://a.example.com/p"
    assert strip_tracking("https://a.example.com/p") == "https://a.example.com/p"


def retry_waits(clock: Clock) -> list[float]:
    """Waits after the first request's 5 s spacing from the robots request."""
    return clock.waits[1:]


def sequence(*replies: httpx.Response | Exception) -> Handler:
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def test_429_with_retry_after_seconds() -> None:
    handler = sequence(httpx.Response(429, headers={"retry-after": "7"}), OK)
    fetcher, net, clock = make_fetcher(handler)
    assert fetcher.get("https://a.example.com/x").body == b"hello"
    assert retry_waits(clock) == [7.0]
    assert len(net.requests) == 2


def test_429_with_retry_after_http_date() -> None:
    date = "Tue, 10 Nov 2026 12:01:35 GMT"
    handler = sequence(httpx.Response(429, headers={"retry-after": date}), OK)
    fetcher, _, clock = make_fetcher(handler)
    fetcher.get("https://a.example.com/x")
    assert retry_waits(clock) == [90.0]


def test_retry_after_in_the_past_waits_zero() -> None:
    date = "Tue, 10 Nov 2026 11:00:00 GMT"
    handler = sequence(httpx.Response(503, headers={"retry-after": date}), OK)
    fetcher, _, clock = make_fetcher(handler)
    fetcher.get("https://a.example.com/x")
    assert retry_waits(clock) == [0.0, 5.0]


def test_long_retry_after_blocks_the_host() -> None:
    fetcher, net, clock = make_fetcher(
        sequence(httpx.Response(429, headers={"retry-after": "3600"}))
    )
    with pytest.raises(HostBlocked):
        fetcher.get("https://a.example.com/x")
    assert retry_waits(clock) == []
    assert fetcher.blocked_hosts() == {"a.example.com"}
    sent = len(net.requests)
    with pytest.raises(HostBlocked):
        fetcher.get("https://a.example.com/y")
    assert len(net.requests) == sent


def test_503_four_times_backs_off_then_blocks() -> None:
    fetcher, net, clock = make_fetcher(sequence(httpx.Response(503)))
    with pytest.raises(HostBlocked) as info:
        fetcher.get("https://a.example.com/x")
    assert retry_waits(clock) == [30.0, 60.0, 120.0]
    assert len(net.requests) == 4
    assert info.value.status == 503
    assert "a.example.com" in fetcher.blocked_hosts()


def test_unparseable_retry_after_uses_backoff() -> None:
    handler = sequence(httpx.Response(429, headers={"retry-after": "soon"}), OK)
    fetcher, _, clock = make_fetcher(handler)
    fetcher.get("https://a.example.com/x")
    assert retry_waits(clock) == [30.0]


def test_connect_timeout_then_ok_retries_once() -> None:
    fetcher, net, clock = make_fetcher(sequence(httpx.ConnectTimeout("t"), OK))
    assert fetcher.get("https://a.example.com/x").status == 200
    assert retry_waits(clock) == [30.0]
    assert len(net.requests) == 2


def test_connect_errors_exhaust_and_block() -> None:
    fetcher, _, clock = make_fetcher(sequence(httpx.ConnectError("c")))
    with pytest.raises(HostBlocked):
        fetcher.get("https://a.example.com/x")
    assert retry_waits(clock) == [30.0, 60.0, 120.0]


def test_403_blocks_host_and_other_hosts_still_work() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403) if request.url.host == "a.example.com" else OK

    fetcher, net, clock = make_fetcher(handler)
    with pytest.raises(HostBlocked):
        fetcher.get("https://a.example.com/x")
    sent = len(net.requests)
    with pytest.raises(HostBlocked):
        fetcher.get("https://a.example.com/y")
    assert len(net.requests) == sent
    assert fetcher.get("https://b.example.com/x").status == 200
    assert clock.waits == [5.0, 5.0]


@pytest.mark.parametrize("status", [401, 451])
def test_401_and_451_block_the_host(status: int) -> None:
    fetcher, _, _ = make_fetcher(sequence(httpx.Response(status)))
    with pytest.raises(HostBlocked):
        fetcher.get("https://a.example.com/x")


@pytest.mark.parametrize("status", [400, 404, 410])
def test_other_4xx_are_not_retried(status: int) -> None:
    fetcher, net, clock = make_fetcher(sequence(httpx.Response(status)))
    with pytest.raises(FetchError) as info:
        fetcher.get("https://a.example.com/x")
    assert not isinstance(info.value, HostBlocked)
    assert len(net.requests) == 1
    assert retry_waits(clock) == []
    assert fetcher.blocked_hosts() == frozenset()


def robots_text(text: str) -> Handler:
    return lambda request: httpx.Response(200, text=text)


def test_robots_disallow_blocks_path_without_a_request() -> None:
    fetcher, net, _ = make_fetcher(
        lambda r: OK, robots=robots_text("User-agent: *\nDisallow: /news/")
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/news/x")
    assert net.requests == []
    assert fetcher.get("https://a.example.com/other").status == 200


def test_robots_group_for_our_token_wins() -> None:
    text = "User-agent: refinery-watch\nDisallow: /\n\nUser-agent: *\nAllow: /"
    fetcher, net, _ = make_fetcher(lambda r: OK, robots=robots_text(text))
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/x")
    assert net.requests == []


@pytest.mark.parametrize("status", [404, 410])
def test_robots_404_and_410_allow_all(status: int) -> None:
    fetcher, _, _ = make_fetcher(lambda r: OK, robots=lambda r: httpx.Response(status))
    assert fetcher.get("https://a.example.com/anything").status == 200


def test_empty_robots_allows_all() -> None:
    fetcher, _, _ = make_fetcher(lambda r: OK, robots=robots_text(""))
    assert fetcher.get("https://a.example.com/anything").status == 200


@pytest.mark.parametrize("status", [500, 503, 401, 403, 302])
def test_robots_failure_disallows_everything_for_the_run(status: int) -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK, robots=lambda r: httpx.Response(status))
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/x")
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/y")
    assert net.requests == []
    assert fetcher.blocked_hosts() == frozenset()


def test_robots_connect_error_disallows_everything() -> None:
    def robots(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("c")

    fetcher, net, _ = make_fetcher(lambda r: OK, robots=robots)
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/x")
    assert net.requests == []


def test_robots_fetched_once_per_host() -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK)
    fetcher.get("https://a.example.com/x")
    fetcher.get("https://a.example.com/y")
    fetcher.get("https://b.example.com/y")
    assert [r.url.host for r in net.robots_requests] == ["a.example.com", "b.example.com"]


def test_robots_request_is_not_redirected_and_uses_our_agent() -> None:
    fetcher, net, _ = make_fetcher(
        lambda r: OK, robots=lambda r: httpx.Response(301, headers={"location": "/elsewhere"})
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/x")
    assert len(net.robots_requests) == 1
    assert "refinery-watch/0.1" in net.robots_requests[0].headers["user-agent"]


def test_redirect_hop_is_checked_against_its_own_robots() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example.com":
            return httpx.Response(302, headers={"location": "https://b.example.org/private"})
        return OK

    def robots(request: httpx.Request) -> httpx.Response:
        if request.url.host == "b.example.org":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        return httpx.Response(404)

    fetcher, net, _ = make_fetcher(handler, robots=robots)
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/x")
    assert [r.url.host for r in net.requests] == ["a.example.com"]


def test_robots_applies_to_query_paths() -> None:
    fetcher, net, _ = make_fetcher(
        lambda r: OK, robots=robots_text("User-agent: *\nDisallow: /rss")
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/rss?feed=news")
    assert net.requests == []


def test_crawl_delay_is_honoured() -> None:
    robots = robots_text("User-agent: *\nCrawl-delay: 12")
    fetcher, _, clock = make_fetcher(lambda r: OK, robots=robots)
    fetcher.get("https://a.example.com/x")
    fetcher.get("https://a.example.com/y")
    assert clock.waits == [12.0, 12.0]


def test_no_crawl_delay_waits_the_remainder_of_five_seconds() -> None:
    fetcher, _, clock = make_fetcher(lambda r: OK)
    fetcher.get("https://a.example.com/x")
    assert clock.waits == [5.0]
    clock.t += 1.0
    fetcher.get("https://a.example.com/y")
    assert clock.waits == [5.0, 4.0]


def test_no_wait_when_the_gap_has_passed() -> None:
    fetcher, _, clock = make_fetcher(lambda r: OK)
    fetcher.get("https://a.example.com/x")
    clock.t += 60.0
    fetcher.get("https://a.example.com/y")
    assert clock.waits == [5.0]


def test_two_hosts_do_not_wait_for_each_other() -> None:
    fetcher, _, clock = make_fetcher(lambda r: OK)
    fetcher.get("https://a.example.com/x")
    fetcher.get("https://b.example.com/x")
    assert clock.waits == [5.0, 5.0]


def test_the_robots_request_counts_as_a_request() -> None:
    fetcher, net, clock = make_fetcher(lambda r: OK)
    fetcher.get("https://a.example.com/x")
    assert len(net.robots_requests) == 1
    assert clock.waits == [5.0]


def test_redirect_hops_to_one_host_are_spaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return OK

    fetcher, _, clock = make_fetcher(handler)
    fetcher.get("https://a.example.com/a")
    assert clock.waits == [5.0, 5.0]
