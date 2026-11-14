import json
import logging

import httpx
import pytest
from tests.mail.fake_resend import FakeResend

from refinery_watch.mail.resend import (
    ATTEMPTS,
    BACKOFF_S,
    RESEND_URL,
    USER_AGENT,
    Email,
    ResendClient,
    fingerprint,
)

API_KEY = "re_test_value_for_unit_tests"
ADDRESS = "x@y.com"


def email(**over: object) -> Email:
    fields: dict[str, object] = {
        "sender": "Desk <from@example.com>",
        "to": ("a@example.com", "b@example.com"),
        "subject": "Subject",
        "html": "<p>Body</p>",
        "text": "Body",
    }
    fields.update(over)
    return Email(**fields)  # type: ignore[arg-type]


def client(fake: FakeResend) -> tuple[ResendClient, list[float]]:
    waits: list[float] = []
    return ResendClient(API_KEY, transport=fake.transport(), sleep=waits.append), waits


def test_one_request_with_headers_and_fields() -> None:
    fake = FakeResend()
    c, _ = client(fake)
    result = c.send(email(), "k" * 32)
    assert (result.ok, result.message_id, result.attempts, result.error) == (
        True,
        "msg_0001",
        1,
        None,
    )
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert (request.method, str(request.url)) == ("POST", RESEND_URL)
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["idempotency-key"] == "k" * 32
    assert request.headers["user-agent"] == USER_AGENT == "refinery-watch/0.1"
    assert request.headers["content-type"] == "application/json"
    assert sorted(json.loads(request.content)) == ["from", "html", "subject", "text", "to"]
    assert json.loads(request.content)["to"] == ["a@example.com", "b@example.com"]


def test_server_error_then_ok_resends_identical_bytes() -> None:
    fake = FakeResend([httpx.Response(500, json={"name": "internal_server_error"})])
    fake.script.append(httpx.Response(200, json={"id": "m2"}))
    c, waits = client(fake)
    result = c.send(email(), "key-1")
    assert (result.ok, result.attempts, result.message_id) == (True, 2, "m2")
    assert len(fake.requests) == 2
    assert fake.requests[0].content == fake.requests[1].content
    assert {r.headers["idempotency-key"] for r in fake.requests} == {"key-1"}
    assert waits == [BACKOFF_S[0]]


def test_429_waits_for_retry_after() -> None:
    fake = FakeResend([httpx.Response(429, headers={"Retry-After": "5"})])
    fake.script.append(httpx.Response(200, json={"id": "m"}))
    c, waits = client(fake)
    assert c.send(email(), "k").ok
    assert waits == [5.0]


def test_429_with_long_retry_after_uses_the_backoff() -> None:
    fake = FakeResend([httpx.Response(429, headers={"Retry-After": "600"})])
    fake.script.append(httpx.Response(200, json={"id": "m"}))
    c, waits = client(fake)
    assert c.send(email(), "k").ok
    assert waits == [BACKOFF_S[0]]


def test_three_timeouts_give_up() -> None:
    fake = FakeResend([httpx.ReadTimeout("slow")])
    c, waits = client(fake)
    result = c.send(email(), "k")
    assert (result.ok, result.attempts, result.status) == (False, ATTEMPTS, None)
    assert result.error == "ReadTimeout"
    assert waits == list(BACKOFF_S)


def test_transport_error_is_retried_and_named() -> None:
    fake = FakeResend([httpx.ConnectError("no route to api.resend.com")])
    c, _ = client(fake)
    result = c.send(email(), "k")
    assert result.error == "ConnectError"
    assert len(fake.requests) == ATTEMPTS


def test_five_hundreds_exhaust_the_attempts() -> None:
    fake = FakeResend([httpx.Response(503, json={"name": "service_unavailable"})])
    c, _ = client(fake)
    result = c.send(email(), "k")
    assert (result.ok, result.attempts, result.status) == (False, ATTEMPTS, 503)
    assert result.error == "503 service_unavailable"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    fake = FakeResend([httpx.Response(status, json={"name": "invalid_idempotent_request"})])
    c, waits = client(fake)
    result = c.send(email(), "k")
    assert (result.ok, result.attempts, result.status) == (False, 1, status)
    assert len(fake.requests) == 1
    assert waits == []


def test_error_keeps_the_name_and_drops_the_message() -> None:
    body = {"name": "validation_error", "message": f"bad {ADDRESS}"}
    fake = FakeResend([httpx.Response(422, json=body)])
    c, _ = client(fake)
    result = c.send(email(), "k")
    assert result.error == "422 validation_error"
    assert ADDRESS not in repr(result)


@pytest.mark.parametrize("name", ["Has Space", "x" * 65, "UPPER", "a@b.com", 7])
def test_odd_error_names_are_replaced(name: object) -> None:
    fake = FakeResend([httpx.Response(400, json={"name": name})])
    c, _ = client(fake)
    assert c.send(email(), "k").error == "400 error"


def test_non_json_error_body() -> None:
    fake = FakeResend([httpx.Response(400, content=b"<html>nope</html>")])
    c, _ = client(fake)
    assert c.send(email(), "k").error == "400 error"


@pytest.mark.parametrize("body", [{}, {"id": ""}, {"id": 5}, []])
def test_200_without_an_id_is_a_failure(body: object) -> None:
    fake = FakeResend([httpx.Response(200, json=body)])
    c, _ = client(fake)
    result = c.send(email(), "k")
    assert (result.ok, result.error, result.attempts) == (False, "200 no_id", 1)


def test_200_with_a_non_json_body_is_a_failure() -> None:
    fake = FakeResend([httpx.Response(200, content=b"ok")])
    c, _ = client(fake)
    result = c.send(email(), "k")
    assert (result.ok, result.error) == (False, "200 no_id")


def test_no_log_record_holds_the_key_or_an_address(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeResend([httpx.Response(500, json={"name": "boom", "message": ADDRESS})])
    fake.script.append(httpx.Response(200, json={"id": "m"}))
    c, _ = client(fake)
    c.send(email(to=(ADDRESS,)), "k")
    text = caplog.text + repr(c)
    assert API_KEY not in text
    assert ADDRESS not in text


def test_the_key_is_not_in_the_repr() -> None:
    assert API_KEY not in repr(ResendClient(API_KEY, transport=FakeResend().transport()))


def test_fingerprint_ignores_recipient_order_but_not_case() -> None:
    a = email(to=("A@example.com", "b@example.com"))
    b = email(to=("b@example.com", "A@example.com"))
    assert fingerprint(a) == fingerprint(b)
    assert len(fingerprint(a)) == 64
    assert fingerprint(a) != fingerprint(email(to=("a@example.com", "b@example.com")))


def test_fingerprint_changes_with_recipients_and_sender() -> None:
    base = fingerprint(email())
    assert fingerprint(email(to=("a@example.com",))) != base
    assert fingerprint(email(sender="Other <o@example.com>")) != base


def test_fingerprint_ignores_the_subject_and_body() -> None:
    assert fingerprint(email(subject="Other", text="x", html="y")) == fingerprint(email())


@pytest.mark.parametrize(
    "over",
    [
        {"to": ()},
        {"to": tuple(f"u{i}@example.com" for i in range(51))},
        {"subject": "a\r\nBcc: z@example.com"},
        {"sender": "a@example.com\nBcc: z@example.com"},
        {"to": ("a@example.com\r\n",)},
    ],
)
def test_email_refuses_bad_fields(over: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"recipients|one line"):
        email(**over)


def test_fifty_recipients_are_allowed() -> None:
    assert len(email(to=tuple(f"u{i}@example.com" for i in range(50))).to) == 50


@pytest.mark.parametrize("key", ["", "k" * 257])
def test_idempotency_key_length_is_checked(key: str) -> None:
    c, _ = client(FakeResend())
    with pytest.raises(ValueError, match="idempotency key"):
        c.send(email(), key)


def test_a_real_transport_is_blocked_in_mail_tests() -> None:
    with (
        httpx.Client(transport=httpx.HTTPTransport()) as http,
        pytest.raises(AssertionError, match="blocked"),
    ):
        http.get("https://api.resend.com/emails")


def test_context_manager_closes() -> None:
    with ResendClient(API_KEY, transport=FakeResend().transport()) as c:
        assert c.send(email(), "k").ok


def test_a_retry_that_passes_the_fingerprint_check_sends_the_same_bytes() -> None:
    first = email(to=("a@example.test", "b@example.test"))
    later = email(to=("b@example.test", "a@example.test"))
    assert fingerprint(first) == fingerprint(later)
    fake = FakeResend()
    c, _ = client(fake)
    c.send(first, "key-1")
    c.send(later, "key-1")
    assert fake.requests[0].content == fake.requests[1].content
    assert json.loads(fake.requests[0].content)["to"] == ["a@example.test", "b@example.test"]
