"""Fixtures shared by the infra tests."""

import pytest


@pytest.fixture
def valid_repo_urls() -> tuple[str, ...]:
    """Clone URLs that the repo_url validation accepts."""
    return (
        "https://github.com/owner/repo.git",
        "https://github.com/Owner-1/re.po_x-y.git",
    )
