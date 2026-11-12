import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_CSV_SHA256 = "47069e94244b74818770098451aac8be63e7c99f26e3b7897e09bfa23a4b59ec"


def test_sources_csv_matches_pinned_hash() -> None:
    raw = (ROOT / "data" / "sources.csv").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == SOURCES_CSV_SHA256, (
        "data/sources.csv changed: review the edit, then update SOURCES_CSV_SHA256"
    )
