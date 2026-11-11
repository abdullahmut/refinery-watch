"""Raw document storage on any S3 endpoint, through the minio client."""

from io import BytesIO
from typing import BinaryIO, Final, Protocol

import urllib3
from minio import Minio
from minio.error import MinioException

from refinery_watch.config import ObjectStoreSettings

KEY_PREFIX: Final = "raw/"


class ObjectStoreError(RuntimeError):
    """The store refused or lost a request. The text holds the key and an error code only."""

    def __init__(self, key: str, code: str) -> None:
        self.key = key
        self.code = code
        super().__init__(f"object store error key={key} code={code}")


class RawStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...


class PutClient(Protocol):
    """The part of the minio client used here."""

    def put_object(
        self, bucket_name: str, object_name: str, data: BinaryIO, length: int, content_type: str
    ) -> object: ...


class S3RawStore:
    def __init__(self, settings: ObjectStoreSettings, client: PutClient | None = None) -> None:
        self._settings = settings
        self._client: PutClient = client or Minio(
            settings.host,
            access_key=settings.access_key,
            secret_key=settings.secret_key,
            secure=settings.secure,
            region=settings.region,
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if not key.startswith(KEY_PREFIX):
            raise ValueError(f"object key must start with {KEY_PREFIX}")
        code: str | None = None
        try:
            self._client.put_object(
                self._settings.bucket_raw, key, BytesIO(data), len(data), content_type
            )
        except (MinioException, urllib3.exceptions.HTTPError, OSError) as exc:
            code = str(getattr(exc, "code", None) or type(exc).__name__)
        # Raised outside the except block so the client error, which can hold URLs, is not chained.
        if code is not None:
            raise ObjectStoreError(key, code)
