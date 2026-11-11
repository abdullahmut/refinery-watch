"""Polite HTTP fetcher shared by every source adapter."""

import ipaddress
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final, Self
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

UA_TOKEN: Final = "refinery-watch"  # noqa: S105
TIMEOUT: Final = httpx.Timeout(20.0, pool=5.0)
MAX_RETRIES: Final = 3
BACKOFF_S: Final = (30.0, 60.0, 120.0)
MAX_RETRY_AFTER_S: Final = 300.0
BLOCKING_STATUSES: Final = frozenset({401, 403, 451})
MAX_BYTES: Final = 5_000_000
MAX_REDIRECTS: Final = 5
ALLOWED_PORTS: Final = (80, 443)


def user_agent(contact_email: str) -> str:
    return f"{UA_TOKEN}/0.1 (+mailto:{contact_email})"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes
    fetched_at: datetime


class FetchError(RuntimeError):
    """A fetch failed. The text holds the host and path, never a query string."""

    def __init__(self, reason: str, url: str, status: int | None = None) -> None:
        parts = urlsplit(url)
        self.host = (parts.hostname or "").lower()
        self.reason = reason
        self.status = status
        super().__init__(f"{reason} host={self.host} path={parts.path}")


class RobotsDisallowed(FetchError):  # noqa: N818
    """robots.txt does not allow the path."""


class HostBlocked(FetchError):  # noqa: N818
    """The host gets no more requests in this run."""


class AddressRefused(FetchError):  # noqa: N818
    """The URL is not plain http(s) on a public address."""


Resolver = Callable[[str], list[str]]


def resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _charset(content_type_header: str) -> str | None:
    for part in content_type_header.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip("\"'").lower() or None
    return None


RETRYABLE_FAILURES: Final = frozenset({"timeout", "network_error"})


@dataclass(slots=True)
class _Reply:
    status: int
    headers: httpx.Headers
    body: bytes


@dataclass(frozen=True, slots=True)
class _Robots:
    """Parsed rules for one origin. A parser of None means every path is disallowed."""

    parser: RobotFileParser | None


@dataclass(frozen=True, slots=True)
class _Stop:
    """A request that ended without a usable reply."""

    reason: str
    status: int | None
    block: bool


class Fetcher:
    def __init__(
        self,
        contact_email: str,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve: Resolver = resolve_host,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = httpx.Client(
            transport=transport,
            timeout=TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": user_agent(contact_email)},
        )
        self._resolve = resolve
        self._monotonic = monotonic
        self._sleep = sleep
        self._now = now
        self._blocked: set[str] = set()
        self._robots: dict[str, _Robots] = {}

    def blocked_hosts(self) -> frozenset[str]:
        return frozenset(self._blocked)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        params: Sequence[tuple[str, str]] = (),
        accept: str = "*/*",
    ) -> FetchResult:
        current = urlsplit(url)._replace(fragment="").geturl()
        send_params = params
        for _hop in range(MAX_REDIRECTS + 1):
            self._check_target(current)
            self._check_robots(current)
            reply = self._send(current, send_params, accept)
            location = reply.headers.get("location")
            if 300 <= reply.status < 400 and location:
                current = urlsplit(urljoin(current, location))._replace(fragment="").geturl()
                send_params = ()
                continue
            if reply.status != 200:
                raise FetchError("http_status", current, reply.status)
            ctype = reply.headers.get("content-type", "")
            return FetchResult(
                url=current,
                status=reply.status,
                content_type=ctype.split(";", 1)[0].strip().lower(),
                charset=_charset(ctype),
                body=reply.body,
                fetched_at=self._now(),
            )
        raise FetchError("too_many_redirects", current)

    def _check_target(self, url: str) -> None:
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            raise AddressRefused("bad_url", url)
        if port is not None and port not in ALLOWED_PORTS:
            raise AddressRefused("bad_port", url)
        if host in self._blocked:
            raise HostBlocked("host_blocked", url)
        try:
            addresses = self._resolve(host)
        except OSError:
            addresses = []
        if not addresses or not all(_public(a) for a in addresses):
            raise AddressRefused("non_public_address", url)

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        rules = self._robots.get(origin)
        if rules is None:
            rules = self._robots[origin] = self._fetch_robots(origin)
        if rules.parser is None or not rules.parser.can_fetch(UA_TOKEN, url):
            raise RobotsDisallowed("robots_disallowed", url)

    def _fetch_robots(self, origin: str) -> _Robots:
        outcome = self._attempts(f"{origin}/robots.txt", (), "text/plain")
        if isinstance(outcome, _Reply) and outcome.status in (200, 404, 410):
            parser = RobotFileParser()
            if outcome.status == 200:
                parser.parse(outcome.body.decode("utf-8", errors="replace").splitlines())
            else:
                parser.parse([])
            return _Robots(parser)
        return _Robots(None)

    def _send(self, url: str, params: Sequence[tuple[str, str]], accept: str) -> _Reply:
        outcome = self._attempts(url, params, accept)
        if isinstance(outcome, _Stop):
            if outcome.block:
                self._blocked.add((urlsplit(url).hostname or "").lower())
                raise HostBlocked(outcome.reason, url, outcome.status)
            raise FetchError(outcome.reason, url, outcome.status)
        return outcome

    def _attempts(self, url: str, params: Sequence[tuple[str, str]], accept: str) -> _Reply | _Stop:
        for attempt in range(MAX_RETRIES + 1):
            outcome = self._send_once(url, params, accept)
            retry_after: float | None = None
            if isinstance(outcome, _Reply):
                if outcome.status in BLOCKING_STATUSES:
                    return _Stop("http_status", outcome.status, True)
                if outcome.status != 429 and outcome.status < 500:
                    return outcome
                status: int | None = outcome.status
                retry_after = self._retry_after(outcome.headers.get("retry-after"))
            elif outcome in RETRYABLE_FAILURES:
                status = None
            else:
                return _Stop(outcome, None, False)
            if retry_after is not None and retry_after > MAX_RETRY_AFTER_S:
                return _Stop("retry_after_too_long", status, True)
            if attempt == MAX_RETRIES:
                return _Stop("retries_exhausted", status, True)
            self._sleep(retry_after if retry_after is not None else BACKOFF_S[attempt])
        raise AssertionError("unreachable")

    def _retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        text = value.strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - self._now()).total_seconds())

    def _send_once(self, url: str, params: Sequence[tuple[str, str]], accept: str) -> _Reply | str:
        failure: str | None = None
        reply: _Reply | None = None
        try:
            request = self._client.build_request(
                "GET", url, params=list(params) or None, headers={"Accept": accept}
            )
            response = self._client.send(request, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        failure = "too_large"
                        break
                    chunks.append(chunk)
                if failure is None:
                    reply = _Reply(response.status_code, response.headers, b"".join(chunks))
            finally:
                response.close()
        except httpx.TimeoutException:
            failure = "timeout"
        except httpx.HTTPError:
            failure = "network_error"
        except (httpx.InvalidURL, httpx.StreamError):
            failure = "bad_request"
        if reply is None:
            return failure or "network_error"
        return reply
