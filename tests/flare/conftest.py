"""Flare tests never reach the network."""

import pytest
from tests.netguard import block_network
from tests.sources.conftest import real_fixture as real_fixture


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_network(monkeypatch)
