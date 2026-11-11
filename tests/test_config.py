from pathlib import Path

import pytest

from refinery_watch.config import (
    ConfigError,
    contact_email,
    database_url,
    eia_api_key,
    object_store_settings,
    refineries_csv_path,
)


def test_database_url_unset_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        database_url()


def test_database_url_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1/db")
    assert database_url() == "postgresql://x@127.0.0.1/db"


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
