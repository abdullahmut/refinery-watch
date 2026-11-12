"""Resend client over httpx. One POST per send, retried with the same key and the same bytes."""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, Self

import httpx

RESEND_URL: Final = "https://api.resend.com/emails"
TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)
ATTEMPTS: Final = 3
BACKOFF_S: Final = (10.0, 30.0)
MAX_RETRY_AFTER_S: Final = 60.0
USER_AGENT: Final = "refinery-watch/0.1"
MAX_RECIPIENTS: Final = 50
MAX_KEY_CHARS: Final = 256
_ERROR_NAME: Final = re.compile(r"[a-z_]{1,64}")
_HEADER_UNSAFE: Final = re.compile(r"[\r\n]")


@dataclass(frozen=True, slots=True)
class Email:
    sender: str
    to: tuple[str, ...]
    subject: str
    html: str
    text: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.to) <= MAX_RECIPIENTS:
            raise ValueError(f"an email needs 1 to {MAX_RECIPIENTS} recipients")
        for value in (self.sender, self.subject, *self.to):
            if _HEADER_UNSAFE.search(value):
                raise ValueError("sender, recipients and subject must be one line")


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    message_id: str | None
    status: int | None  # last HTTP status, None for a transport error
    error: str | None  # "<status> <name>" or "<exception class>"; no key, body or address
    attempts: int


class Mailer(Protocol):
    def send(self, email: Email, idempotency_key: str) -> SendResult: ...


def _ordered(addresses: tuple[str, ...]) -> list[str]:
    """Sorted without regard to case, case kept. The fingerprint and the payload both use it."""
    return sorted(addresses, key=lambda a: (a.lower(), a))


def fingerprint(email: Email) -> str:
    """Hash of the sender and the sorted recipients, so a changed list is noticed."""
    recipients = ",".join(_ordered(email.to))
    return hashlib.sha256(f"{email.sender}\n{recipients}".encode()).hexdigest()


def _payload(email: Email) -> bytes:
    body = {
        "from": email.sender,
        "to": _ordered(email.to),
        "subject": email.subject,
        "html": email.html,
        "text": email.text,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _error_name(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return "error"
    name = data.get("name") if isinstance(data, dict) else None
    if isinstance(name, str) and _ERROR_NAME.fullmatch(name):
        return name
    return "error"


def _message_id(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= MAX_RETRY_AFTER_S else None


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class ResendClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=TIMEOUT, transport=transport, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return "ResendClient()"

    def send(self, email: Email, idempotency_key: str) -> SendResult:
        if not 1 <= len(idempotency_key) <= MAX_KEY_CHARS:
            raise ValueError(f"the idempotency key needs 1 to {MAX_KEY_CHARS} characters")
        content = _payload(email)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": idempotency_key,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        result = SendResult(False, None, None, "no attempt", 0)
        for attempt in range(1, ATTEMPTS + 1):
            result, wait = self._once(content, headers, attempt)
            if wait is None or attempt == ATTEMPTS:
                return result
            self._sleep(wait)
        return result

    def _once(
        self, content: bytes, headers: dict[str, str], attempt: int
    ) -> tuple[SendResult, float | None]:
        """One request. The second item is the wait before a retry, None when not retryable."""
        backoff = BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1]
        try:
            response = self._client.post(RESEND_URL, content=content, headers=headers)
        except httpx.TransportError as exc:  # includes timeouts
            return SendResult(False, None, None, type(exc).__name__, attempt), backoff
        status = response.status_code
        if status == 200:
            message_id = _message_id(response)
            if message_id is None:
                return SendResult(False, None, 200, "200 no_id", attempt), None
            return SendResult(True, message_id, 200, None, attempt), None
        error = f"{status} {_error_name(response)}"
        failed = SendResult(False, None, status, error, attempt)
        if not _retryable(status):
            return failed, None
        wait = _retry_after(response) if status == 429 else None
        return failed, backoff if wait is None else wait

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
