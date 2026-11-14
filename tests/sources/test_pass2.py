"""Regression tests for the second round of findings. Offline: no socket is opened."""

import socket
import ssl
import typing

import httpcore
import httpx
import pytest
from tests.sources.helpers import EMAIL, Net, make_fetcher

from refinery_watch.sources import eia_wpsr
from refinery_watch.sources.clean import clean_text
from refinery_watch.sources.http import (
    Fetcher,
    FetchError,
    PinnedTransport,
    RobotsDisallowed,
    resolve_host,
)
from refinery_watch.sources.robots import parse_robots

V6 = "2606:2800:220:1:248:1893:25c8:1946"
V4 = "93.184.216.34"
NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
TOKEN = "refinery-watch"  # noqa: S105


class _Stream(httpcore.MockStream):
    def __init__(self, log: list[str], buffer: list[bytes]) -> None:
        super().__init__(buffer)
        self._log = log

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self._log.append(f"tls {server_hostname}")
        return self


class _Backend(httpcore.MockBackend):
    """Records TCP connects and TLS handshakes. Refuses IPv6, as on an IPv4-only Droplet."""

    def __init__(self) -> None:
        super().__init__([])
        self.log: list[str] = []

    def connect_tcp(
        self, host: str, port: int, *a: typing.Any, **k: typing.Any
    ) -> httpcore.NetworkStream:
        if ":" in host:
            raise httpcore.ConnectError("network unreachable")
        self.log.append(f"tcp {host}")
        return _Stream(self.log, [NOT_FOUND] * 20)


def _wire(fetcher: Fetcher, backend: _Backend, *hosts: str) -> None:
    transport = typing.cast(PinnedTransport, fetcher._client._transport)
    for host in hosts:
        inner = typing.cast(httpx.HTTPTransport, transport.transport_for(host))
        inner._pool._network_backend = backend


def _getaddrinfo(order: typing.Sequence[tuple[int, str]]) -> typing.Callable[..., list[object]]:
    def fake(host: str, *a: object, **k: object) -> list[object]:
        out: list[object] = []
        for family, address in order:
            sockaddr = (address, 0) if family == socket.AF_INET else (address, 0, 0, 0)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    return fake


def test_dual_stack_host_is_reached_over_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", _getaddrinfo([(socket.AF_INET6, V6), (socket.AF_INET, V4)])
    )
    fetcher = Fetcher(EMAIL, sleep=lambda s: None)
    backend = _Backend()
    _wire(fetcher, backend, "a.example.com")
    with pytest.raises(FetchError) as info:
        fetcher.get("https://a.example.com/x")
    assert info.value.status == 404, (info.value.reason, backend.log)
    assert "tcp " + V4 in backend.log


def test_each_host_gets_its_own_tls_handshake() -> None:
    fetcher = Fetcher(EMAIL, resolve=lambda host: [V4], sleep=lambda s: None)
    backend = _Backend()
    _wire(fetcher, backend, "a.example.com", "b.example.com")
    for host in ("a.example.com", "b.example.com"):
        with pytest.raises(FetchError):
            fetcher.get(f"https://{host}/x")
    assert "tls a.example.com" in backend.log
    assert "tls b.example.com" in backend.log


def test_resolve_keeps_getaddrinfo_order_and_ipv4_only_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[tuple[int, str]] = [
        (socket.AF_INET6, V6),
        (socket.AF_INET, "93.184.216.34"),
        (socket.AF_INET, "8.8.8.8"),
        (socket.AF_INET, "93.184.216.34"),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo(order))
    assert resolve_host("a.example.com") == ["93.184.216.34", "8.8.8.8"]
    assert resolve_host("a.example.com", ipv6=True) == [V6, "93.184.216.34", "8.8.8.8"]


def test_ipv6_flag_reaches_the_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", _getaddrinfo([(socket.AF_INET6, V6), (socket.AF_INET, V4)])
    )
    seen: list[str] = []
    net = Net(lambda r: httpx.Response(200, content=b"x"), lambda r: httpx.Response(404))
    on = Fetcher(EMAIL, transport=httpx.MockTransport(net), ipv6=True, sleep=lambda s: None)
    on.get("https://a.example.com/x")
    seen.extend(str(r.extensions["pinned_ip"]) for r in net.requests)
    assert seen == [V6]


def _fail_first_address(first: str) -> typing.Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.extensions["pinned_ip"] == first:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=b"x")

    return handler


def test_a_failed_connect_falls_back_to_the_next_validated_address() -> None:
    resolved: list[str] = []

    def resolve(host: str) -> list[str]:
        resolved.append(host)
        return [V4, "93.184.216.35"]

    fetcher = Fetcher(
        EMAIL,
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(404) if r.url.path == "/robots.txt" else _fail_first_address(V4)(r)
            )
        ),
        resolve=resolve,
        sleep=lambda s: None,
    )
    assert fetcher.get("https://a.example.com/x").body == b"x"
    assert resolved == ["a.example.com"]


def test_fallback_never_uses_an_address_that_was_not_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    seen: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.extensions["pinned_ip"]))
        return handler(request)

    fetcher = Fetcher(
        EMAIL,
        transport=httpx.MockTransport(recording),
        resolve=lambda host: [V4, "93.184.216.35"],
        sleep=lambda s: None,
    )
    with pytest.raises(FetchError):
        fetcher.get("https://a.example.com/x")
    assert set(seen) == {V4, "93.184.216.35"}


def test_empty_path_with_query_is_checked_as_root() -> None:
    sent: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        sent.append(str(request.url))
        return httpx.Response(200, content=b"x")

    fetcher = Fetcher(
        EMAIL,
        transport=httpx.MockTransport(recording),
        resolve=lambda host: [V4],
        sleep=lambda s: None,
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com?p=1")
    assert sent == []


@pytest.mark.parametrize(
    ("rule", "target"),
    [
        ("/~joe/", "/%7Ejoe/index.html"),
        ("/foo/bar/ツ", "/foo/bar/%E3%83%84"),
        ("/foo/bar/%E3%83%84", "/foo/bar/ツ"),
        ("/a%2fb", "/a%2Fb"),
        ("/%41bc", "/Abc"),
    ],
)
def test_rfc9309_percent_encoding_equivalence(rule: str, target: str) -> None:
    rules = parse_robots(f"User-agent: *\nDisallow: {rule}\n", TOKEN)
    assert not rules.allows(target)


def test_an_encoded_slash_is_not_a_slash() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /a/b\n", TOKEN)
    assert rules.allows("/a%2Fb")


def test_anchored_wildcard_needs_room_for_both_parts() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /a*a$\n", TOKEN)
    assert rules.allows("/a")
    assert not rules.allows("/aba")


@pytest.mark.parametrize("delay", ["61", "3600", "inf", "nan", "1e999"])
def test_a_crawl_delay_over_sixty_seconds_disallows_the_host(delay: str) -> None:
    fetcher, net, clock = make_fetcher(
        lambda r: httpx.Response(200, content=b"x"),
        robots=lambda r: httpx.Response(200, text=f"User-agent: *\nCrawl-delay: {delay}\n"),
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://a.example.com/x")
    assert net.requests == []
    assert clock.waits == []


def test_a_crawl_delay_of_sixty_seconds_is_kept() -> None:
    fetcher, _, clock = make_fetcher(
        lambda r: httpx.Response(200, content=b"x"),
        robots=lambda r: httpx.Response(200, text="User-agent: *\nCrawl-delay: 60\n"),
    )
    fetcher.get("https://a.example.com/x")
    assert clock.waits == [60.0]


def test_eia_run_is_held_to_a_deadline() -> None:
    fetcher, net, clock = make_fetcher(lambda r: httpx.Response(200, content=b"{}"))
    outcome = eia_wpsr.run(
        None,  # type: ignore[arg-type]
        fetcher,
        None,  # type: ignore[arg-type]
        1,
        "https://eia.example.com/v2/x",
        "KEY1",
        clock.t + 2.0,
    )
    assert outcome.status == "failed"
    assert outcome.note is not None
    assert "BudgetExceeded" in outcome.note
    assert net.requests == []


def test_bidi_controls_are_stripped() -> None:
    marks = [0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
    text = "a" + "".join(chr(c) for c in marks) + "b"
    assert clean_text(text) == "a b"


def test_resolve_family_flag_is_belt_and_braces(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def fake(host: str, port: object, family: int = 0, *a: object, **k: object) -> list[object]:
        seen.append(family)
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (V6, 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (V4, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert resolve_host("a.example.com") == [V4]
    assert seen == [socket.AF_INET]
