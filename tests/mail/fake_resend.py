"""Two fakes: a MockTransport handler that plays Resend, and a Mailer that records sends."""

from collections.abc import Callable, Sequence

import httpx

from refinery_watch.mail.resend import Email, SendResult

OK_BODY = {"id": "msg_0001"}


class FakeResend:
    """An httpx handler. `script` gives one response per request; the last one repeats."""

    def __init__(self, script: Sequence[httpx.Response | Exception] | None = None) -> None:
        self.script: list[httpx.Response | Exception] = list(script or [])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self.script)) - 1
        if index < 0:
            return httpx.Response(200, json=OK_BODY)
        step = self.script[index]
        if isinstance(step, Exception):
            raise step
        return step

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class FakeMailer:
    """Records each send. `results` are used in order; the last one repeats."""

    def __init__(self, results: Sequence[SendResult] | None = None) -> None:
        self.results = list(results or [])
        self.sent: list[tuple[Email, str]] = []
        self.on_send: Callable[[Email, str], None] | None = None

    def send(self, email: Email, idempotency_key: str) -> SendResult:
        self.sent.append((email, idempotency_key))
        if self.on_send is not None:
            self.on_send(email, idempotency_key)
        if not self.results:
            return SendResult(True, f"msg_{len(self.sent):04d}", 200, None, 1)
        return self.results[min(len(self.sent), len(self.results)) - 1]


def failed(status: int = 500, name: str = "internal_server_error") -> SendResult:
    return SendResult(False, None, status, f"{status} {name}", 3)
