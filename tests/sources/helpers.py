"""Shared builders for fetcher tests. No test here opens a socket or resolves a name."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from refinery_watch.sources.http import Fetcher

PUBLIC = "93.184.216.34"
EMAIL = "contact@example.com"
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    """A fake clock whose sleep advances time and records the wait."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.t += seconds

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t - 1000.0)


class Net:
    """Records requests and answers them from a handler."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def make_fetcher(
    handler: Handler,
    *,
    addresses: dict[str, list[str]] | None = None,
    clock: Clock | None = None,
) -> tuple[Fetcher, Net, Clock]:
    net = Net(handler)
    clk = clock or Clock()
    table = addresses or {}

    def resolve(host: str) -> list[str]:
        return table.get(host, [PUBLIC])

    fetcher = Fetcher(
        EMAIL,
        transport=httpx.MockTransport(net),
        resolve=resolve,
        monotonic=clk.monotonic,
        sleep=clk.sleep,
        now=clk.now,
    )
    return fetcher, net, clk
