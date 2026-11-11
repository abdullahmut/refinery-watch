from typing import BinaryIO

import pytest
from minio.error import ServerError

from refinery_watch import objects
from refinery_watch.config import ObjectStoreSettings
from refinery_watch.objects import ObjectStoreError, S3RawStore

SECRET = "secret-value-xyz"  # noqa: S105


def settings(endpoint: str = "https://store.example.com") -> ObjectStoreSettings:
    return ObjectStoreSettings(
        endpoint_url=endpoint,
        region="test-1",
        bucket_raw="rw-raw-test",
        access_key="access-id",
        secret_key=SECRET,
    )


class StubClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, bytes, int, str]] = []
        self.error = error

    def put_object(
        self, bucket_name: str, object_name: str, data: BinaryIO, length: int, content_type: str
    ) -> object:
        if self.error is not None:
            raise self.error
        self.calls.append((bucket_name, object_name, data.read(), length, content_type))
        return object()


def test_put_calls_the_client_once_with_bucket_from_settings() -> None:
    client = StubClient()
    S3RawStore(settings(), client).put("raw/hp_news/2026/11/10/ab.html", b"<p>hi</p>", "text/html")
    assert client.calls == [
        ("rw-raw-test", "raw/hp_news/2026/11/10/ab.html", b"<p>hi</p>", 9, "text/html")
    ]


def test_key_without_prefix_is_refused_before_any_call() -> None:
    client = StubClient()
    with pytest.raises(ValueError, match="raw/"):
        S3RawStore(settings(), client).put("hp_news/x.html", b"x", "text/html")
    assert client.calls == []


def test_client_error_becomes_object_store_error_without_secrets() -> None:
    client = StubClient(ServerError(f"boom {SECRET} https://store.example.com/?X-Amz=1", 500))
    with pytest.raises(ObjectStoreError) as info:
        S3RawStore(settings(), client).put("raw/a/b.html", b"x", "text/html")
    text = str(info.value) + repr(info.value)
    assert SECRET not in text
    assert "X-Amz" not in text
    assert "raw/a/b.html" in text
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_os_error_is_wrapped_too() -> None:
    client = StubClient(ConnectionResetError("reset"))
    with pytest.raises(ObjectStoreError, match="ConnectionResetError"):
        S3RawStore(settings(), client).put("raw/a/b.html", b"x", "text/html")


def test_constructor_passes_region_and_tls_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeMinio:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.append((args, kwargs))

        def put_object(
            self, bucket_name: str, object_name: str, data: BinaryIO, length: int, content_type: str
        ) -> object:
            return object()

    monkeypatch.setattr(objects, "Minio", FakeMinio)
    S3RawStore(settings())
    S3RawStore(settings("http://10.110.0.2:9000"))
    assert seen[0] == (
        ("store.example.com",),
        {"access_key": "access-id", "secret_key": SECRET, "secure": True, "region": "test-1"},
    )
    assert seen[1][0] == ("10.110.0.2:9000",)
    assert seen[1][1]["secure"] is False
    assert seen[1][1]["region"] == "test-1"
