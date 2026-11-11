import logging

import pytest

from refinery_watch.sources.__main__ import main

ENV = {
    "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",
    "CONTACT_EMAIL": "contact@example.com",
    "OBJECT_STORE_ENDPOINT": "https://store.example.com",
    "OBJECT_STORE_REGION": "test-1",
    "OBJECT_STORE_BUCKET_RAW": "rw-raw-test",
    "OBJECT_STORE_ACCESS_KEY": "access-id",
    "OBJECT_STORE_SECRET_KEY": "secret-value-xyz",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    return monkeypatch


def test_unknown_source_exits_2(env: pytest.MonkeyPatch) -> None:
    assert main(["nope"]) == 2


def test_missing_source_argument_exits_2(env: pytest.MonkeyPatch) -> None:
    assert main([]) == 2


@pytest.mark.parametrize(
    "name", ["DATABASE_URL", "CONTACT_EMAIL", "OBJECT_STORE_BUCKET_RAW", "OBJECT_STORE_ENDPOINT"]
)
def test_missing_setting_exits_2_and_names_it(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    env.delenv(name)
    assert main(["hp_news"]) == 2
    err = capsys.readouterr().err
    assert name in err
    for value in ENV.values():
        assert value not in err


def test_eia_needs_its_key(env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eia_wpsr"]) == 2
    assert "EIA_API_KEY" in capsys.readouterr().err


def test_http_loggers_are_quiet_after_setup(env: pytest.MonkeyPatch) -> None:
    logging.getLogger("httpx").setLevel(logging.INFO)
    main(["nope"])
    for name in ("httpx", "httpcore", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING
