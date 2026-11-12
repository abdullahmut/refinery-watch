"""Settings read from the environment. Nothing here reads a .env file."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _database_url(override_var: str, user_var: str, default_user: str, password_var: str) -> str:
    explicit = os.environ.get(override_var, "")
    if explicit:
        if os.environ.get("PG_HOST") or os.environ.get(password_var):
            raise ConfigError(f"set {override_var} or PG_HOST with {password_var}, not both")
        return explicit
    host = _required("PG_HOST")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ConfigError("PG_HOST must be a host name or IPv4 address")
    port = os.environ.get("PG_PORT") or "5432"
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ConfigError("PG_PORT must be a number from 1 to 65535")
    database = os.environ.get("PG_DATABASE") or "refinery_watch"
    user = os.environ.get(user_var) or default_user
    password = os.environ.get(password_var, "")
    if not password:
        raise ConfigError(f"{password_var} is not set")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def app_database_url() -> str:
    """Build the app role URL from PG_* settings. APP_DATABASE_URL is the only override."""
    return _database_url("APP_DATABASE_URL", "PG_APP_USER", "refinery_app", "PG_APP_PASSWORD")


def owner_database_url() -> str:
    """Build the owner role URL for migrations and seeds.

    OWNER_DATABASE_URL is the only override. PG_OWNER_PASSWORD is supplied in the
    environment at run time by the operator. It is never stored in the app or ingest .env.
    """
    return _database_url(
        "OWNER_DATABASE_URL", "PG_OWNER_USER", "refinery_owner", "PG_OWNER_PASSWORD"
    )


def refineries_csv_path() -> Path:
    path = Path(_required("REFINERIES_CSV_PATH"))
    if not path.is_file():
        raise ConfigError(f"REFINERIES_CSV_PATH is not a file: {path}")
    return path


def contact_email() -> str:
    value = _required("CONTACT_EMAIL")
    local, sep, domain = value.partition("@")
    if not sep or not local or not domain or "@" in domain or re.search(r"\s", value):
        raise ConfigError("CONTACT_EMAIL is not a plain address")
    return value


def fetch_ipv6() -> bool:
    """FETCH_IPV6: 1 or 0, off by default. The Droplets have no IPv6 address."""
    value = os.environ.get("FETCH_IPV6", "0").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("", "0", "false", "no"):
        return False
    raise ConfigError("FETCH_IPV6 must be 1 or 0")


def eia_api_key() -> str:
    value = _required("EIA_API_KEY")
    if not re.fullmatch(r"[A-Za-z0-9]+", value):
        raise ConfigError("EIA_API_KEY must be letters and digits only")
    return value


_REGION = re.compile(r"[a-z0-9-]+")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


@dataclass(frozen=True, slots=True)
class ObjectStoreSettings:
    endpoint_url: str
    region: str
    bucket_raw: str
    access_key: str
    secret_key: str = field(repr=False)

    @property
    def secure(self) -> bool:
        return urlsplit(self.endpoint_url).scheme == "https"

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint_url).netloc


def _endpoint(value: str) -> str:
    bad = ConfigError("OBJECT_STORE_ENDPOINT must be http:// or https:// with a host only")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise bad from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise bad
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise bad
    if parts.username is not None or parts.password is not None or "?" in value or "#" in value:
        raise bad
    return value


def object_store_settings() -> ObjectStoreSettings:
    endpoint = _endpoint(_required("OBJECT_STORE_ENDPOINT"))
    region = _required("OBJECT_STORE_REGION")
    if not _REGION.fullmatch(region):
        raise ConfigError("OBJECT_STORE_REGION must match [a-z0-9-]+")
    bucket = _required("OBJECT_STORE_BUCKET_RAW")
    if not _BUCKET.fullmatch(bucket):
        raise ConfigError("OBJECT_STORE_BUCKET_RAW is not a valid bucket name")
    return ObjectStoreSettings(
        endpoint_url=endpoint,
        region=region,
        bucket_raw=bucket,
        access_key=_required("OBJECT_STORE_ACCESS_KEY"),
        secret_key=_required("OBJECT_STORE_SECRET_KEY"),
    )
