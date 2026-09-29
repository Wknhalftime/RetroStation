"""AUD-009 gate 1 characterisation tests for the library-scan module split.

Locks the CURRENT observable behaviour of ``read_tags``, ``scan_directory``
and ``scan_folder_incrementally`` before ``backend/services/library_scan_service.py``
splits into a tag/file-reading module (``backend/services/audio_tags.py``) and the
scan/reconcile half that stays behind (see audit/triage/findings.jsonl AUD-009).
Only imports change in the move commit — these snapshots
(``__snapshots__/test_library_scan_characterisation.ambr``) must stay
byte-identical across that commit.

No Postgres, no network: everything here runs against the checked-in audio
fixtures (tests/fixtures/audio) or in-memory fakes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from syrupy.assertion import SnapshotAssertion

from backend.domain.enums import FileStatus
from backend.domain.library import AudioMetadata, LibraryFile, LibraryQuarantine
from backend.services.audio_tags import read_tags
from backend.services.library_scan_service import (
    scan_directory,
    scan_folder_incrementally,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository

AUDIO_DIR = Path(__file__).parent.parent / "fixtures" / "audio"


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}")
    return path


def _serialize_audio(audio: AudioMetadata) -> dict[str, Any]:
    """AudioMetadata as a stable dict — volatile fields collapsed to a presence flag."""
    return {
        "recording_mbid": audio.recording_mbid,
        "artist_mbid": audio.artist_mbid,
        "album_artist_mbid": audio.album_artist_mbid,
        "release_mbid": audio.release_mbid,
        "release_title": audio.release_title,
        "release_type": audio.release_type.value if audio.release_type else None,
        "release_type_secondary": audio.release_type_secondary,
        "release_status": audio.release_status.value if audio.release_status else None,
        "track_title": audio.track_title,
        "track_number": audio.track_number,
        "disc_number": audio.disc_number,
        "duration_ms": "present" if audio.duration_ms is not None else None,
        "bitrate": "present" if audio.bitrate is not None else None,
        "artist_name": audio.artist_name,
        "normalized_artist_name": audio.normalized_artist_name,
        "normalized_title": audio.normalized_title,
        "raw_metadata_keys": sorted(audio.raw_metadata) if audio.raw_metadata else [],
    }


def _serialize_library_file(lf: LibraryFile) -> dict[str, Any]:
    """LibraryFile as a stable dict: id, mtime and absolute path normalised."""
    return {
        "file_name": Path(lf.file_path).name,
        "format": lf.format,
        "enrichment_status": lf.enrichment_status.value,
        "file_status": lf.file_status.value,
        "file_size": lf.file_size,
        "file_mtime_ns": "present" if lf.file_mtime_ns is not None else None,
        "audio_hash": str(lf.audio_hash) if lf.audio_hash is not None else None,
        "audio": _serialize_audio(lf.audio),
    }


def _serialize_quarantine(q: LibraryQuarantine) -> dict[str, Any]:
    return {"file_name": Path(q.file_path).name, "error_message": q.error_message}


# ---------------------------------------------------------------------------
# read_tags — every fixture audio file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["well_tagged.mp3", "partial_tags.mp3", "minimal_tags.ogg", "no_tags.wav"],
)
def test_read_tags_snapshot(name: str, snapshot: SnapshotAssertion) -> None:
    path = _require(AUDIO_DIR / name)
    lf = read_tags(path)
    assert _serialize_library_file(lf) == snapshot


# ---------------------------------------------------------------------------
# scan_directory — the whole fixture dir (files + quarantine)
# ---------------------------------------------------------------------------


def test_scan_directory_snapshot(snapshot: SnapshotAssertion) -> None:
    if not AUDIO_DIR.exists():
        pytest.skip("Audio fixtures directory not found")
    files, quarantine = scan_directory(AUDIO_DIR)
    result = {
        "files": sorted(
            (_serialize_library_file(lf) for lf in files),
            key=lambda d: d["file_name"],
        ),
        "quarantine": sorted(
            (_serialize_quarantine(q) for q in quarantine),
            key=lambda d: d["file_name"],
        ),
    }
    assert result == snapshot


# ---------------------------------------------------------------------------
# scan_folder_incrementally — one run touching several scenarios at once,
# against in-memory fakes.
# ---------------------------------------------------------------------------


def test_scan_folder_incrementally_snapshot(tmp_path: Path, snapshot: SnapshotAssertion) -> None:
    folder = tmp_path / "jazz"
    folder.mkdir()

    # Scenario 1 (unchanged): a row whose stored stat matches disk — skipped unread.
    unchanged_path = folder / "unchanged.mp3"
    shutil.copy(_require(AUDIO_DIR / "well_tagged.mp3"), unchanged_path)
    unchanged_stat = unchanged_path.stat()

    # Scenario 2 (modified): a row whose stored stat no longer matches disk.
    changed_path = folder / "changed.ogg"
    shutil.copy(_require(AUDIO_DIR / "minimal_tags.ogg"), changed_path)

    # Scenario 3 (new): a file with no row at all.
    new_path = folder / "new.wav"
    shutil.copy(_require(AUDIO_DIR / "no_tags.wav"), new_path)

    # Scenario 5 (missing): a row for a file no longer on disk.
    ghost_path = folder / "ghost.flac"

    # Scenario 6 (parse failure): a file mutagen cannot read.
    corrupt_path = folder / "corrupt.mp3"
    shutil.copy(_require(AUDIO_DIR / "corrupt.mp3"), corrupt_path)

    file_repo = FakeLibraryFileRepository()
    file_repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(unchanged_path),
            format="mp3",
            file_size=unchanged_stat.st_size,
            file_mtime_ns=unchanged_stat.st_mtime_ns,
        )
    )
    file_repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(changed_path),
            format="ogg",
            file_size=1,
            file_mtime_ns=1,
        )
    )
    file_repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(ghost_path),
            format="flac",
            file_size=1,
            file_mtime_ns=1,
        )
    )

    quarantine_repo = FakeLibraryQuarantineRepository()

    result = scan_folder_incrementally(
        folder_path=folder,
        file_repo=file_repo,
        quarantine_repo=quarantine_repo,
    )

    rows_by_name = {
        Path(lf.file_path).name: _serialize_library_file(lf)
        for lf in file_repo._data.values()  # noqa: SLF001 — test-only introspection of the fake
    }
    snapshot_payload = {
        "result": {
            "files_written": result.files_written,
            "files_skipped": result.files_skipped,
            "files_missing": result.files_missing,
            "files_reappeared": result.files_reappeared,
            "quarantined": result.quarantined,
            "files_relocated": result.files_relocated,
            "folder_unreadable": result.folder_unreadable,
            "quarantine_cleared": result.quarantine_cleared,
        },
        "rows": rows_by_name,
        "quarantine_paths": sorted(
            Path(p).name for p in {e.file_path for e in quarantine_repo.list_all()}
        ),
    }
    assert snapshot_payload == snapshot
    # ghost.flac's row must have been marked MISSING, not dropped.
    assert rows_by_name["ghost.flac"]["file_status"] == FileStatus.MISSING.value
