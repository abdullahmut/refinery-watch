import pytest

from refinery_watch.db import migrate, seed
from refinery_watch.db.seed import SeedError

OWNER_URL = "postgresql://owner@127.0.0.1:1/owner_db"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "APP_DATABASE_URL",
        "OWNER_DATABASE_URL",
        "DATABASE_URL",
        "PG_HOST",
        "PG_APP_PASSWORD",
        "PG_OWNER_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def fail_connect(url: str) -> None:
    raise AssertionError("connect must not be reached")


def test_migrate_with_only_app_override_exits_2(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("APP_DATABASE_URL", "postgresql://app@127.0.0.1:1/db")
    clean_env.setattr(migrate, "upgrade", fail_connect)
    assert migrate.main([]) == 2
    assert "PG_HOST" in capsys.readouterr().err


def test_seed_with_only_app_override_exits_2(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("APP_DATABASE_URL", "postgresql://app@127.0.0.1:1/db")
    clean_env.setattr(seed, "connect", fail_connect)
    assert seed.main(["sources"]) == 2
    assert "PG_HOST" in capsys.readouterr().err


def test_migrate_receives_the_owner_override(clean_env: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(url: str, app_role: str = "", schema: str = "") -> None:
        calls.append(url)

    clean_env.setenv("OWNER_DATABASE_URL", OWNER_URL)
    clean_env.setenv("APP_DATABASE_URL", "postgresql://app@127.0.0.1:1/db")
    clean_env.setattr(migrate, "upgrade", fake)
    assert migrate.main([]) == 0
    assert calls == [OWNER_URL]


def test_seed_receives_the_owner_override(clean_env: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def stop(url: str) -> None:
        calls.append(url)
        raise SeedError("stop")

    clean_env.setenv("OWNER_DATABASE_URL", OWNER_URL)
    clean_env.setenv("APP_DATABASE_URL", "postgresql://app@127.0.0.1:1/db")
    clean_env.setattr(seed, "connect", stop)
    assert seed.main(["sources"]) == 2
    assert calls == [OWNER_URL]
