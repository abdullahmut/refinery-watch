import httpx
import pytest
from tests.sources.helpers import EMAIL, T0, make_fetcher

from refinery_watch.config import ConfigError, contact_email
from refinery_watch.sources.http import (
    TIMEOUT,
    AddressRefused,
    Fetcher,
    FetchError,
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
    fetcher, _, _ = make_fetcher(lambda r: OK)
    assert fetcher._client.timeout == TIMEOUT
    assert (TIMEOUT.connect, TIMEOUT.read, TIMEOUT.write, TIMEOUT.pool) == (20, 20, 20, 5)


def test_result_fields() -> None:
    fetcher, _, _ = make_fetcher(lambda r: OK)
    result = fetcher.get("https://a.example.com/x#frag")
    assert result.url == "https://a.example.com/x"
    assert result.status == 200
    assert result.content_type == "text/html"
    assert result.charset == "utf-8"
    assert result.body == b"hello"
    assert result.fetched_at == T0


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
