"""Makes a real network call fail loudly. Shared by the flare and mail conftests."""

import socket
from typing import Any

import httpx
import pytest

LOCAL = frozenset({"127.0.0.1", "localhost", "::1", ""})


class NetworkBlocked(AssertionError):  # noqa: N818
    """A test tried to reach the network."""


def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def handle_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise NetworkBlocked(f"real HTTP request blocked: {request.url.host}")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in LOCAL:
            raise NetworkBlocked("name lookup blocked")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
