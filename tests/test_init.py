from importlib.metadata import version
from pathlib import Path

import refinery_watch


def test_version_matches_installed_metadata() -> None:
    assert refinery_watch.__version__ == version("refinery-watch")


def test_package_imports_from_src_layout() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    expected = repo_root / "src" / "refinery_watch" / "__init__.py"
    assert Path(refinery_watch.__file__).resolve() == expected
