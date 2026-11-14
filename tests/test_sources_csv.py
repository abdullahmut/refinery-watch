import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_CSV_SHA256 = "02bea3579e07b6a2d38e0dccd16df97a88afd03c16dd91cac9b39ee71b806838"


def test_sources_csv_matches_pinned_hash() -> None:
    raw = (ROOT / "data" / "sources.csv").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == SOURCES_CSV_SHA256, (
        "data/sources.csv changed: review the edit, then update SOURCES_CSV_SHA256"
    )
