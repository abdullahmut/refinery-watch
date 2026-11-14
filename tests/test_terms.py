import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "refinery_watch"


def test_term_matcher_is_declared_once() -> None:
    declared = [
        path
        for path in SRC.rglob("*.py")
        if re.search(r"^TermMatcher\s*(:[^=\n]+)?=", path.read_text(encoding="utf-8"), re.M)
    ]
    assert declared == [SRC / "terms.py"]


def test_terms_module_imports_nothing_from_the_project() -> None:
    text = (SRC / "terms.py").read_text(encoding="utf-8")
    assert "refinery_watch" not in text


def test_match_form_is_not_re_exported_by_keywords() -> None:
    code = "import refinery_watch.extract.keywords as k; print(hasattr(k, 'match_form'))"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"
