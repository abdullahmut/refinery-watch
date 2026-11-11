import socket

import httpx
import pytest
from tests.sources.helpers import EMAIL, PUBLIC, Net, make_fetcher

from refinery_watch.sources.http import (
    AddressRefused,
    Fetcher,
    FetchError,
    PinnedTransport,
    _public,
)

OK = httpx.Response(200, content=b"hello")
METADATA = "169.254.169.254"


def test_rebinding_resolver_is_resolved_once_and_never_reaches_metadata() -> None:
    answers = [[PUBLIC], [METADATA], [METADATA]]
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return answers.pop(0)

    net = Net(lambda r: OK, lambda r: httpx.Response(404))
    fetcher = Fetcher(
        EMAIL, transport=httpx.MockTransport(net), resolve=resolve, sleep=lambda s: None
    )
    fetcher.get("https://a.example.com/x")
    fetcher.get("https://a.example.com/y")
    assert calls == ["a.example.com"]
    seen = [r.extensions["pinned_ip"] for r in net.requests + net.robots_requests]
    assert seen == [PUBLIC] * 3


def test_metadata_address_in_the_same_answer_is_refused() -> None:
    fetcher, net, _ = make_fetcher(lambda r: OK, addresses={"a.example.com": [PUBLIC, METADATA]})
    with pytest.raises(AddressRefused):
        fetcher.get("https://a.example.com/x")
    assert net.requests == []
    assert net.robots_requests == []


def test_retries_and_robots_use_the_same_pinned_address() -> None:
    answers = iter([[PUBLIC], [METADATA], [METADATA], [METADATA]])
    replies = iter([httpx.Response(503), OK])
    net = Net(lambda r: next(replies), lambda r: httpx.Response(404))
    fetcher = Fetcher(
        EMAIL,
        transport=httpx.MockTransport(net),
        resolve=lambda host: next(answers),
        sleep=lambda s: None,
    )
    fetcher.get("https://a.example.com/x")
    everything = net.robots_requests + net.requests
    assert len(everything) == 3
    assert {r.extensions["pinned_ip"] for r in everything} == {PUBLIC}


def test_a_redirect_host_is_validated_and_pinned_on_its_own() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example.com":
            return httpx.Response(302, headers={"location": "https://b.example.org/y"})
        return OK

    fetcher, net, _ = make_fetcher(handler, addresses={"b.example.org": ["93.184.216.35"]})
    fetcher.get("https://a.example.com/x")
    assert [r.extensions["pinned_ip"] for r in net.requests] == [PUBLIC, "93.184.216.35"]


def recording_inner() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    return httpx.MockTransport(handler), seen


def test_transport_connects_to_the_ip_and_keeps_the_name_for_host_and_sni() -> None:
    inner, seen = recording_inner()
    transport = PinnedTransport(inner, {"a.example.com": PUBLIC}, connect_by_ip=True)
    with httpx.Client(transport=transport) as client:
        client.get("https://a.example.com/path?q=1", headers={"X-T": "1"})
    (request,) = seen
    assert request.url.host == PUBLIC
    assert request.url.path == "/path"
    assert request.url.query == b"q=1"
    assert request.headers["host"] == "a.example.com"
    assert request.headers["x-t"] == "1"
    assert request.extensions["sni_hostname"] == "a.example.com"


def test_transport_handles_an_ipv6_pin() -> None:
    inner, seen = recording_inner()
    transport = PinnedTransport(inner, {"a.example.com": "2606:4700::1111"}, connect_by_ip=True)
    with httpx.Client(transport=transport) as client:
        client.get("https://a.example.com/")
    assert seen[0].url.host == "2606:4700::1111"
    assert seen[0].headers["host"] == "a.example.com"


def test_transport_refuses_a_host_that_was_never_validated() -> None:
    inner, seen = recording_inner()
    transport = PinnedTransport(inner, {}, connect_by_ip=True)
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get("https://a.example.com/")
    assert seen == []


def test_the_real_transport_is_pinned_by_default() -> None:
    fetcher = Fetcher(EMAIL)
    transport = fetcher._client._transport
    assert isinstance(transport, PinnedTransport)
    assert transport._connect_by_ip is True
    fetcher.close()


def test_environment_proxies_and_ca_files_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    Fetcher(EMAIL).close()


def _getaddrinfo(record: list[bytes]):  # type: ignore[no-untyped-def]
    def fake(host: str, *args: object, **kw: object) -> list[object]:
        record.append(host.encode("idna"))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]

    return fake


def test_unencodable_host_is_a_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo([]))
    fetcher = Fetcher(
        EMAIL,
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        sleep=lambda s: None,
    )
    with pytest.raises(FetchError):
        fetcher.get("https://www..example.com/story")


def test_guard_resolves_the_name_httpx_connects_to(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved: list[bytes] = []
    connected: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        connected.append(request.url.raw_host)
        return httpx.Response(404)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo(resolved))
    fetcher = Fetcher(EMAIL, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(FetchError):
        fetcher.get("https://straße.example/x")
    assert connected
    assert set(connected) <= set(resolved), (resolved, connected)


def test_gzip_reply_is_refused_and_identity_is_requested() -> None:
    gz = httpx.Response(200, content=iter([b"gzip bytes"]), headers={"content-encoding": "gzip"})
    fetcher, net, _ = make_fetcher(lambda r: gz)
    with pytest.raises(FetchError, match="bad_encoding"):
        fetcher.get("https://a.example.com/x")
    assert net.requests[0].headers["accept-encoding"] == "identity"


def test_identity_encoding_header_is_accepted() -> None:
    ok = httpx.Response(200, content=b"x", headers={"content-encoding": "identity"})
    fetcher, _, _ = make_fetcher(lambda r: ok)
    assert fetcher.get("https://a.example.com/x").body == b"x"


@pytest.mark.parametrize(
    "address",
    [
        "::",
        "::2",
        "::808:808",
        "64:ff9b::808:808",
        "64:ff9b:1::1",
        "2002:808:808::1",
        "::ffff:10.0.0.1",
        "fe80::1",
        "fc00::1",
    ],
)
def test_ipv6_forms_that_embed_ipv4_or_are_local_are_refused(address: str) -> None:
    assert not _public(address)


def test_global_ipv6_is_allowed() -> None:
    assert _public("2606:4700::1111")
    assert _public("2001:4860:4860::8888")
