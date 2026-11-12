"""Flare tests never reach the network."""

import pytest
from tests.netguard import block_network


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_network(monkeypatch)
