import os
from pathlib import Path

import pytest
from tests.sources.conftest import find_fixture


def touch(path: Path, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_newest_match_wins_and_derived_files_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RW_FIXTURES_DIR", str(tmp_path))
    touch(tmp_path / "hp_rss" / "hp_news_20261101.xml", 100)
    touch(tmp_path / "hp_rss" / "hp_news_20261105.xml", 200)
    touch(tmp_path / "hp_rss" / "hp_news_empty_derived_20261109.xml", 300)
    assert find_fixture("hp_rss/hp_news_*.xml") == tmp_path / "hp_rss" / "hp_news_20261105.xml"
    derived = find_fixture("hp_rss/hp_news_empty_derived_*.xml")
    assert derived == tmp_path / "hp_rss" / "hp_news_empty_derived_20261109.xml"


def test_absent_files_give_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RW_FIXTURES_DIR", str(tmp_path))
    assert find_fixture("eia/none_*.json") is None
