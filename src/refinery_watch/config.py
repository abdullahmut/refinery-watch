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


def _database_url(
    *, override_env: str, user_env: str, default_user: str, credential_env: str
) -> str:
    explicit = os.environ.get(override_env, "")
    if explicit:
        if os.environ.get("PG_HOST") or os.environ.get(credential_env):
            raise ConfigError(f"set {override_env} or PG_HOST with {credential_env}, not both")
        return explicit
    host = _required("PG_HOST")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ConfigError("PG_HOST must be a host name or IPv4 address")
    port = os.environ.get("PG_PORT") or "5432"
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ConfigError("PG_PORT must be a number from 1 to 65535")
    database = os.environ.get("PG_DATABASE") or "refinery_watch"
    user = os.environ.get(user_env) or default_user
    password = os.environ.get(credential_env, "")
    if not password:
        raise ConfigError(f"{credential_env} is not set")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def app_database_url() -> str:
    """Build the app role URL from PG_* settings. APP_DATABASE_URL is the only override."""
    return _database_url(
        override_env="APP_DATABASE_URL",
        user_env="PG_APP_USER",
        default_user="refinery_app",
        credential_env="PG_APP_PASSWORD",
    )


def owner_database_url() -> str:
    """Build the owner role URL for migrations and seeds. Only OWNER_DATABASE_URL overrides."""
    return _database_url(
        override_env="OWNER_DATABASE_URL",
        user_env="PG_OWNER_USER",
        default_user="refinery_owner",
        credential_env="PG_OWNER_PASSWORD",
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


_FIRMS_KEY = re.compile(r"[A-Za-z0-9]{20,64}")
_ADDRESS_BAD = re.compile(r"[\s<>,;\"]")
_DISPLAY_NAME = re.compile(r"[A-Za-z0-9 .-]{1,60}")
MAX_ADDRESS_CHARS = 254
MAX_RECIPIENTS = 20
ALERT_DAILY_MAX_DEFAULT = 10
ALERT_DAILY_MAX_RANGE = (1, 50)


def firms_map_key() -> str:
    value = _required("FIRMS_MAP_KEY")
    if not _FIRMS_KEY.fullmatch(value):
        raise ConfigError("FIRMS_MAP_KEY must be 20 to 64 letters and digits")
    return value


def refinery_locations_csv_path() -> Path:
    path = Path(_required("REFINERY_LOCATIONS_CSV_PATH"))
    if not path.is_file():
        raise ConfigError(f"REFINERY_LOCATIONS_CSV_PATH is not a file: {path}")
    return path


def _is_address(value: str) -> bool:
    local, sep, domain = value.partition("@")
    return (
        bool(sep)
        and bool(local)
        and "@" not in domain
        and "." in domain
        and len(value) <= MAX_ADDRESS_CHARS
        and _ADDRESS_BAD.search(value) is None
    )


def note_recipients() -> tuple[str, ...]:
    """NOTE_RECIPIENTS: comma-separated addresses, de-duplicated without regard to case."""
    raw = os.environ.get("NOTE_RECIPIENTS", "")
    items = [item.strip() for item in raw.split(",")] if raw.strip() else []
    if not items or not all(_is_address(item) for item in items):
        raise ConfigError("NOTE_RECIPIENTS must be 1 to 20 plain addresses, comma-separated")
    seen: dict[str, str] = {}
    for item in items:
        seen.setdefault(item.lower(), item)
    if len(seen) > MAX_RECIPIENTS:
        raise ConfigError("NOTE_RECIPIENTS must be 1 to 20 plain addresses, comma-separated")
    return tuple(seen.values())


def note_from() -> str:
    """NOTE_FROM: an address, or Display Name <address>."""
    value = _required("NOTE_FROM")
    address = value
    if "<" in value or ">" in value:
        name, sep, rest = value.partition("<")
        if not sep or not rest.endswith(">") or not _DISPLAY_NAME.fullmatch(name.strip()):
            raise ConfigError("NOTE_FROM must be an address or Name <address>")
        address = rest[:-1]
    if not _is_address(address):
        raise ConfigError("NOTE_FROM must be an address or Name <address>")
    return value


def resend_api_key() -> str:
    value = _required("RESEND_API_KEY")
    if re.search(r"\s", value):
        raise ConfigError("RESEND_API_KEY must not contain whitespace")
    return value


def flare_alert_daily_max() -> int:
    raw = os.environ.get("FLARE_ALERT_DAILY_MAX", "").strip()
    if not raw:
        return ALERT_DAILY_MAX_DEFAULT
    low, high = ALERT_DAILY_MAX_RANGE
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not low <= int(raw) <= high:
        raise ConfigError("FLARE_ALERT_DAILY_MAX must be a whole number from 1 to 50")
    return int(raw)
