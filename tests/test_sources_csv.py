import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_CSV_SHA256 = "4c78192a6c85a9867d2082e9a5876b9f3a203d5e0006ccf738fc143e6afc9ea0"


def test_sources_csv_matches_pinned_hash() -> None:
    raw = (ROOT / "data" / "sources.csv").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == SOURCES_CSV_SHA256, (
        "data/sources.csv changed: review the edit, then update SOURCES_CSV_SHA256"
    )
