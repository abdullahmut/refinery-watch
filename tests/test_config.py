from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from refinery_watch.config import (
    ConfigError,
    app_database_url,
    contact_email,
    eia_api_key,
    fetch_ipv6,
    object_store_settings,
    owner_database_url,
    refineries_csv_path,
)

PG_VARS = (
    "APP_DATABASE_URL",
    "OWNER_DATABASE_URL",
    "DATABASE_URL",
    "PG_HOST",
    "PG_PORT",
    "PG_DATABASE",
    "PG_APP_USER",
    "PG_APP_PASSWORD",
    "PG_OWNER_USER",
    "PG_OWNER_PASSWORD",
)


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in PG_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_url_built_with_defaults(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("PG_HOST", "10.0.0.5")
    pg_env.setenv("PG_APP_PASSWORD", "pw")
    assert app_database_url() == "postgresql://refinery_app:pw@10.0.0.5:5432/refinery_watch"


def test_url_built_from_all_pg_settings(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("PG_HOST", "db.internal")
    pg_env.setenv("PG_PORT", "5434")
    pg_env.setenv("PG_DATABASE", "other")
    pg_env.setenv("PG_APP_USER", "someone")
    pg_env.setenv("PG_APP_PASSWORD", "pw")
    assert app_database_url() == "postgresql://someone:pw@db.internal:5434/other"


def test_password_special_characters_are_quoted(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("PG_HOST", "h")
    pg_env.setenv("PG_APP_PASSWORD", "p@ss:w/o rd?#%")
    url = app_database_url()
    assert url == "postgresql://refinery_app:p%40ss%3Aw%2Fo%20rd%3F%23%25@h:5432/refinery_watch"
    assert unquote(urlsplit(url).password or "") == "p@ss:w/o rd?#%"


@pytest.mark.parametrize("missing", ["PG_HOST", "PG_APP_PASSWORD"])
def test_url_missing_value_names_variable(pg_env: pytest.MonkeyPatch, missing: str) -> None:
    pg_env.setenv("PG_HOST", "h")
    pg_env.setenv("PG_APP_PASSWORD", "secret-pw")
    pg_env.delenv(missing)
    with pytest.raises(ConfigError, match=missing) as info:
        app_database_url()
    assert "secret-pw" not in str(info.value)


@pytest.mark.parametrize("other", ["PG_HOST", "PG_APP_PASSWORD"])
def test_url_override_with_other_setting_raises(pg_env: pytest.MonkeyPatch, other: str) -> None:
    pg_env.setenv("APP_DATABASE_URL", "postgresql://x@127.0.0.1/db")
    pg_env.setenv(other, "secret-pw")
    with pytest.raises(ConfigError, match="not both") as info:
        app_database_url()
    assert "secret-pw" not in str(info.value)


def test_app_override_alone_is_returned(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("APP_DATABASE_URL", "postgresql://x@127.0.0.1/db")
    assert app_database_url() == "postgresql://x@127.0.0.1/db"


def test_plain_database_url_is_ignored(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("DATABASE_URL", "postgresql://x@127.0.0.1/db")
    with pytest.raises(ConfigError, match="PG_HOST"):
        app_database_url()
    with pytest.raises(ConfigError, match="PG_HOST"):
        owner_database_url()


def test_app_override_cannot_reach_owner_path(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("APP_DATABASE_URL", "postgresql://app@127.0.0.1/db")
    with pytest.raises(ConfigError, match="PG_HOST"):
        owner_database_url()


def test_owner_override_cannot_reach_app_path(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("OWNER_DATABASE_URL", "postgresql://owner@127.0.0.1/db")
    with pytest.raises(ConfigError, match="PG_HOST"):
        app_database_url()


@pytest.mark.parametrize("port", ["0", "65536", "99999", "123456"])
def test_port_out_of_range_rejected(pg_env: pytest.MonkeyPatch, port: str) -> None:
    pg_env.setenv("PG_HOST", "h")
    pg_env.setenv("PG_APP_PASSWORD", "pw")
    pg_env.setenv("PG_PORT", port)
    with pytest.raises(ConfigError, match="PG_PORT"):
        app_database_url()


@pytest.mark.parametrize(("name", "value"), [("PG_PORT", "54x"), ("PG_HOST", "a/b")])
def test_url_bad_value_rejected(pg_env: pytest.MonkeyPatch, name: str, value: str) -> None:
    pg_env.setenv("PG_HOST", "h")
    pg_env.setenv("PG_APP_PASSWORD", "pw")
    pg_env.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        app_database_url()


def test_owner_url_built_with_defaults(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("PG_HOST", "10.0.0.5")
    pg_env.setenv("PG_OWNER_PASSWORD", "o@pw")
    pg_env.setenv("PG_APP_PASSWORD", "app-pw")
    assert owner_database_url() == "postgresql://refinery_owner:o%40pw@10.0.0.5:5432/refinery_watch"


def test_owner_url_uses_pg_owner_user(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("PG_HOST", "h")
    pg_env.setenv("PG_PORT", "5434")
    pg_env.setenv("PG_OWNER_USER", "boss")
    pg_env.setenv("PG_OWNER_PASSWORD", "pw")
    assert owner_database_url() == "postgresql://boss:pw@h:5434/refinery_watch"


def test_owner_url_missing_password_names_variable(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("PG_HOST", "h")
    pg_env.setenv("PG_APP_PASSWORD", "app-pw")
    with pytest.raises(ConfigError, match="PG_OWNER_PASSWORD") as info:
        owner_database_url()
    assert "app-pw" not in str(info.value)


@pytest.mark.parametrize("other", ["PG_HOST", "PG_OWNER_PASSWORD"])
def test_owner_override_with_other_setting_raises(pg_env: pytest.MonkeyPatch, other: str) -> None:
    pg_env.setenv("OWNER_DATABASE_URL", "postgresql://x@127.0.0.1/db")
    pg_env.setenv(other, "secret-pw")
    with pytest.raises(ConfigError, match="not both") as info:
        owner_database_url()
    assert "secret-pw" not in str(info.value)


def test_owner_override_alone_is_returned(pg_env: pytest.MonkeyPatch) -> None:
    pg_env.setenv("OWNER_DATABASE_URL", "postgresql://x@127.0.0.1/db")
    assert owner_database_url() == "postgresql://x@127.0.0.1/db"


def test_refineries_csv_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFINERIES_CSV_PATH", raising=False)
    with pytest.raises(ConfigError, match="REFINERIES_CSV_PATH"):
        refineries_csv_path()


def test_refineries_csv_path_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(tmp_path / "nope.csv"))
    with pytest.raises(ConfigError, match="REFINERIES_CSV_PATH"):
        refineries_csv_path()


def test_refineries_csv_path_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(tmp_path))
    with pytest.raises(ConfigError):
        refineries_csv_path()


def test_refineries_csv_path_returned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "r.csv"
    f.write_text("x\n")
    monkeypatch.setenv("REFINERIES_CSV_PATH", str(f))
    assert refineries_csv_path() == f


def test_contact_email_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    with pytest.raises(ConfigError, match="CONTACT_EMAIL"):
        contact_email()


@pytest.mark.parametrize("value", ["a b@c.d", "x@y\r\nX: 1", "nobody", "a@@b.c", "@b.c", "a@"])
def test_contact_email_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", value)
    with pytest.raises(ConfigError, match="CONTACT_EMAIL") as info:
        contact_email()
    assert value not in str(info.value)


def test_contact_email_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", "contact@example.com")
    assert contact_email() == "contact@example.com"


def test_eia_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="EIA_API_KEY"):
        eia_api_key()
    monkeypatch.setenv("EIA_API_KEY", "ab&cd")
    with pytest.raises(ConfigError, match="EIA_API_KEY") as info:
        eia_api_key()
    assert "ab&cd" not in str(info.value)
    monkeypatch.setenv("EIA_API_KEY", "AbC123")
    assert eia_api_key() == "AbC123"


STORE_ENV = {
    "OBJECT_STORE_ENDPOINT": "https://store.example.com",
    "OBJECT_STORE_REGION": "test-1",
    "OBJECT_STORE_BUCKET_RAW": "rw-raw-test",
    "OBJECT_STORE_ACCESS_KEY": "access-id",
    "OBJECT_STORE_SECRET_KEY": "secret-value-xyz",
}


@pytest.fixture
def store_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in STORE_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.mark.parametrize("name", sorted(STORE_ENV))
def test_object_store_each_unset(store_env: pytest.MonkeyPatch, name: str) -> None:
    store_env.delenv(name)
    with pytest.raises(ConfigError, match=name) as info:
        object_store_settings()
    for value in STORE_ENV.values():
        assert value not in str(info.value)


def test_object_store_https(store_env: pytest.MonkeyPatch) -> None:
    s = object_store_settings()
    assert s.secure is True
    assert s.host == "store.example.com"
    assert s.bucket_raw == "rw-raw-test"


def test_object_store_http_with_port(store_env: pytest.MonkeyPatch) -> None:
    store_env.setenv("OBJECT_STORE_ENDPOINT", "http://10.110.0.2:9000")
    s = object_store_settings()
    assert s.secure is False
    assert s.host == "10.110.0.2:9000"


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://x",
        "store.example.com",
        "https://u:p@x",
        "https://x/raw",
        "https://x?a=1",
        "https://x#f",
        "https://x?",
        "https://x:notaport",
    ],
)
def test_object_store_endpoint_refused(store_env: pytest.MonkeyPatch, endpoint: str) -> None:
    store_env.setenv("OBJECT_STORE_ENDPOINT", endpoint)
    with pytest.raises(ConfigError, match="OBJECT_STORE_ENDPOINT"):
        object_store_settings()


def test_object_store_trailing_slash_ok(store_env: pytest.MonkeyPatch) -> None:
    store_env.setenv("OBJECT_STORE_ENDPOINT", "https://x.example.com/")
    assert object_store_settings().host == "x.example.com"


def test_object_store_bucket_and_region_refused(store_env: pytest.MonkeyPatch) -> None:
    store_env.setenv("OBJECT_STORE_BUCKET_RAW", "Raw_Docs")
    with pytest.raises(ConfigError, match="OBJECT_STORE_BUCKET_RAW"):
        object_store_settings()
    store_env.setenv("OBJECT_STORE_BUCKET_RAW", "rw-raw-test")
    store_env.setenv("OBJECT_STORE_REGION", "US East")
    with pytest.raises(ConfigError, match="OBJECT_STORE_REGION"):
        object_store_settings()


def test_object_store_repr_hides_secret(store_env: pytest.MonkeyPatch) -> None:
    assert STORE_ENV["OBJECT_STORE_SECRET_KEY"] not in repr(object_store_settings())


def test_no_minio_names_in_code() -> None:
    root = Path(__file__).resolve().parents[1]
    needle = "MINIO" + "_"
    for folder in ("src", "tests"):
        for path in (root / folder).rglob("*.py"):
            assert needle not in path.read_text(encoding="utf-8"), path


def test_fetch_ipv6_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FETCH_IPV6", raising=False)
    assert fetch_ipv6() is False
    monkeypatch.setenv("FETCH_IPV6", "0")
    assert fetch_ipv6() is False


def test_fetch_ipv6_can_be_turned_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_IPV6", "1")
    assert fetch_ipv6() is True


def test_fetch_ipv6_rejects_other_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_IPV6", "maybe")
    with pytest.raises(ConfigError, match="FETCH_IPV6"):
        fetch_ipv6()
