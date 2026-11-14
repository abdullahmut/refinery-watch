"""Flare tests never reach the network."""

import pytest
from tests.db.conftest import app_url, owner_url, pg_server, seeded_schema
from tests.netguard import block_network
from tests.sources.conftest import real_fixture as real_fixture

__all__ = ["app_url", "owner_url", "pg_server", "real_fixture", "seeded_schema"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_network(monkeypatch)
