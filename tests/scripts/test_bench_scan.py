"""Guards for scripts/bench_scan.py that need no running scan."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def bench_scan() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("bench_scan")


def test_calls_since_reports_only_timers_that_ran(bench_scan: ModuleType) -> None:
    before = {"file.read_tags": 10, "file.sha256": 10}
    after = {"file.read_tags": 25, "file.sha256": 10, "file.audio_sha256": 3}

    assert bench_scan.calls_since(before, after) == {
        "file.read_tags": 15,
        "file.audio_sha256": 3,
    }


@pytest.mark.integration
def test_hash_coverage_counts_rows_carrying_a_fingerprint(
    bench_scan: ModuleType,
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db) as conn:
        conn.execute(
            "INSERT INTO library_files (file_path, file_hash, format) VALUES (%s, %s, %s)",
            ("/m/a.flac", "h" * 64, "flac"),
        )
        conn.execute(
            "INSERT INTO library_files (file_path, format) VALUES (%s, %s)",
            ("/m/b.flac", "flac"),
        )

    assert bench_scan.hash_coverage(migrated_db)["file_hash"] == 1


def test_the_audio_sha256_hot_spot_exists() -> None:
    from backend.services import audio_hash

    assert callable(audio_hash._sha256_of_range)
