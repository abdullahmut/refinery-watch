import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_CSV_SHA256 = "899913a93a9c465c272f3876c1bac198a39df8bb8a7482e9a6cc463caf2f6f7e"


def test_sources_csv_matches_pinned_hash() -> None:
    raw = (ROOT / "data" / "sources.csv").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == SOURCES_CSV_SHA256, (
        "data/sources.csv changed: review the edit, then update SOURCES_CSV_SHA256"
    )
