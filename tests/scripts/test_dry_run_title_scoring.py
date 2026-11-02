"""Acceptance: scripts/dry_run_title_scoring.py shows trial mode's effect without writing
(SPEC 2026-11-01, S12).

Before trial mode ships, Lance reads which songs it would move. The script scores every song a
matching run would score, with today's scorer and in trial mode, through a read-only
connection, and writes three CSV reports:
- frozen_check.csv: the product's V11.1 against the frozen v11.py, pair by pair (``--frozen``;
  without it, the header only);
- changed_songs.csv: every song whose trial-mode outcome or file differs from today's;
- accepted_songs.csv: where each song Lance matched by hand lands in trial mode.
It also prints the average V11.1 time per pair (E6). Its contents are checked by Lance; this test
checks the reports exist, name the right songs and that nothing is written.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)

pytestmark = pytest.mark.integration

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_REPORTS = {"frozen_check.csv", "changed_songs.csv", "accepted_songs.csv"}
_TABLES = ("broadcast_artists", "track_identities", "library_files", "matches")


@pytest.fixture(scope="module")
def dry_run() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("dry_run_title_scoring")


def _seed(conn: psycopg.Connection[Any]) -> None:
    """One resolved artist with untagged files (Step C only, no MusicBrainz call): "Big Log"
    auto-matched to "Big Love", which trial mode sends to review, and "Heartbeat", which Lance
    matched by hand to "Heart Beat"."""
    artist_id = uuid4()
    name = normalize_artist("Robert Plant")
    conn.execute(
        """INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status)
           VALUES (%s, 'Robert Plant', %s, 'auto_matched')""",
        (artist_id, name),
    )
    # An artist MBID that no file carries: Step A finds nothing and Step B has nothing to add.
    conn.execute(
        """INSERT INTO matches (artist_id, target_id, target_type, confidence_score, match_tier)
           VALUES (%s, %s, 'artist', 100, 'musicbrainz_id_exact')""",
        (artist_id, str(uuid4())),
    )
    files: dict[str, UUID] = {}
    for title in ("Big Love", "Ship Of Fools", "Heart Beat"):
        files[title] = uuid4()
        conn.execute(
            """INSERT INTO library_files (id, file_path, track_title, normalized_title,
                   artist_name, normalized_artist_name, enrichment_status)
               VALUES (%s, %s, %s, %s, 'Robert Plant', %s, 'enriched')""",
            (files[title], f"D:\\Music\\{title}.flac", title, normalize_title(title), name),
        )
    for title, status, file_title in (
        ("Big Log", "auto_matched", "Big Love"),
        ("Heartbeat", "manual_matched", "Heart Beat"),
    ):
        song_id = uuid4()
        norm = normalize_title(title)
        conn.execute(
            """INSERT INTO track_identities (id, broadcast_artist_id, original_title,
                   normalized_title, normalized_signature, match_status, match_tier)
               VALUES (%s, %s, %s, %s, %s, %s, 'local_file_fuzzy')""",
            (song_id, artist_id, title, norm, compute_normalized_signature(name, norm), status),
        )
        conn.execute(
            """INSERT INTO matches (identity_id, library_file_id, confidence_score, match_tier)
               VALUES (%s, %s, 80, 'local_file_fuzzy')""",
            (song_id, files[file_title]),
        )


def _counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _TABLES:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        assert row is not None
        counts[table] = row[0]
    return counts


def test_reports_without_writes(dry_run: ModuleType, migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _seed(conn)
        before = _counts(conn)

    assert dry_run.main(["--database-url", migrated_db, "--out", str(tmp_path)]) == 0

    assert {p.name for p in tmp_path.iterdir()} == _REPORTS
    changed = (tmp_path / "changed_songs.csv").read_text(encoding="utf-8")
    assert "Big Log" in changed
    assert "Heartbeat" not in changed  # a hand-decided song is not re-scored
    assert "Heartbeat" in (tmp_path / "accepted_songs.csv").read_text(encoding="utf-8")
    with psycopg.connect(migrated_db) as conn:
        assert _counts(conn) == before


def test_connection_is_read_only(dry_run: ModuleType, migrated_db: str) -> None:
    with (
        dry_run.connect_read_only(migrated_db) as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute(
            "INSERT INTO broadcast_artists (original_name, normalized_name) VALUES ('x', 'x')"
        )
