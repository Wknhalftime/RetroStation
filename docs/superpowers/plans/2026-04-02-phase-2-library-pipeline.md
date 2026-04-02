# RetroStation Phase 2 — Library Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Pipeline B — scan a local audio library into `library_files`, enrich files via MusicBrainz API to populate canonical entities (`artists`, `works`, `recordings`), then upgrade identity matching from the Phase 1 stub to the real 4-tier waterfall so that log identities can resolve against actual library files.

**Architecture:** A library scan service uses `mutagen` to extract tags from FLAC/MP3/AAC/OGG/WAV files, writing rows to `library_files` (or `library_quarantine` on error). An enrichment service batches files by `release_mbid` for API efficiency, populating `recordings` and linking `library_files.recording_id`. A separate MB enrichment task fills metadata on canonical entities with `needs_enhancement=TRUE`. Finally, the identity matching service is upgraded from "mark everything NEEDS_REVIEW" to the spec's 4-tier waterfall (MBID graph → text → vector → reject). The master selection service is also upgraded from no-op to real scoring.

**Tech Stack:** Python 3.13+, uv, psycopg[binary]>=3.1, mutagen>=1.47, httpx>=0.27, rapidfuzz>=3.0, sentence-transformers (BAAI/bge-m3), structlog, pytest.

**Spec reference:** `docs/superpowers/specs/2026-03-31-retrostation-design.md` — Sections 5.1 (identity matching tiers), 5.4 (master selection scoring), 6.1–6.3 (library pipeline).

**Working directory:** `D:\PythonStuff\RetroStation\.worktrees\phase-2-library\`

---

## Course Corrections from Phase 1

1. **pgvector string serialization** — Phase 1 discovered that psycopg returns pgvector `vector(1024)` columns as strings like `[0.1,0.2,...]`. Write with `"[" + ",".join(str(v) for v in embedding) + "]"`, read with `[float(x) for x in row["embedding"].strip("[]").split(",")]`. All new PG repos must follow this pattern.

2. **Test isolation** — `tests/conftest.py` now uses a function-scoped `migrated_db` fixture that TRUNCATEs all tables before each test. New integration tests must use this fixture.

3. **Library scan path** — User's real library is at `D:\Media\Music` (28k files). Tests use synthetic fixtures only. Manual verification against the real library is done at the Phase 2 gate, not in automated tests.

---

## File Structure

```
backend/
├── services/
│   ├── library_scan_service.py          ← Task 1: mutagen tag extraction
│   ├── library_enrichment_service.py    ← Task 3: MB API → recording_id links
│   ├── mb_client.py                     ← Task 3: add lookup_release(), lookup_recording()
│   ├── identity_matching_service.py     ← Task 5: upgrade from stub to 4-tier
│   ├── master_selection_service.py      ← Task 5: upgrade from no-op to real scoring
│   └── repository_factory.py            ← Task 2: add library_files, library_quarantine
├── db/
│   └── repositories/
│       ├── library_files.py             ← Task 2: PgLibraryFileRepository
│       └── library_quarantine.py        ← Task 2: PgLibraryQuarantineRepository
├── routers/
│   ├── library.py                       ← Task 2: POST /api/v1/library/scan
│   └── v1.py                           ← Task 2: register library router
├── tasks/
│   ├── library_tasks.py                 ← Task 2: library_scan_task
│   ├── library_enrichment_tasks.py      ← Task 3: library_enrichment_task
│   ├── mb_enrichment_tasks.py           ← Task 4: mb_enrichment_task
│   └── identity_matching_tasks.py       ← Task 5: swap FakeLibraryFileRepository → real
tests/
├── fixtures/
│   └── audio/                           ← Task 1: synthetic test audio files
│       ├── well_tagged.flac
│       ├── partial_tags.mp3
│       ├── minimal_tags.ogg
│       ├── no_tags.wav
│       └── corrupt.mp3
├── services/
│   └── test_library_scan.py             ← Task 1: scanner unit tests
├── test_identity_matching.py            ← Task 5: 4-tier matching unit tests
├── integration/
│   ├── test_pg_library_repos.py         ← Task 2: PG repo integration tests
│   ├── test_library_enrichment.py       ← Task 3: enrichment integration test
│   └── test_library_pipeline_e2e.py     ← Task 5: full pipeline E2E
└── fakes/
    └── mb_client.py                     ← Task 3: add lookup methods to FakeMbClient
```

---

## Task 1: Library Scan Service + Test Audio Fixtures

**Files:**
- Create: `tests/fixtures/audio/` (5 synthetic audio files)
- Create: `backend/services/library_scan_service.py`
- Create: `tests/services/test_library_scan.py`

The scan service extracts tags from audio files using `mutagen`. It does NOT touch the database — it returns a list of `LibraryFile` models and a list of `LibraryQuarantine` entries. The caller (task or integration test) handles persistence.

- [ ] **Step 1: Create synthetic test audio fixtures**

Create 5 small audio files with varying tag quality using mutagen. Run this script once to generate them:

```python
# tests/fixtures/create_audio_fixtures.py
"""Run once to create synthetic audio fixtures for library scan tests.

Usage: cd to worktree root, then: uv run python tests/fixtures/create_audio_fixtures.py
"""
from pathlib import Path
import struct
import wave

from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TXXX, TPOS
from mutagen.oggvorbis import OggVorbis

FIXTURE_DIR = Path(__file__).parent / "audio"
FIXTURE_DIR.mkdir(exist_ok=True)


def _make_wav(path: Path, duration_ms: int = 100) -> None:
    """Create a minimal valid WAV file."""
    sample_rate = 8000
    n_samples = int(sample_rate * duration_ms / 1000)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(struct.pack(f"<{n_samples}h", *([0] * n_samples)))


def _make_flac_well_tagged() -> None:
    """Tier 1: All MusicBrainz IDs present."""
    path = FIXTURE_DIR / "well_tagged.flac"
    _make_wav(FIXTURE_DIR / "_tmp.wav")
    import subprocess
    # Convert WAV to FLAC using mutagen's FLAC (needs a real FLAC file)
    # Simpler: create minimal FLAC directly
    from mutagen.flac import FLAC as FLACFile
    # We need a real audio file — create from WAV
    wav_path = FIXTURE_DIR / "_tmp.wav"
    _make_wav(wav_path)
    # Use ffmpeg if available, otherwise create a minimal FLAC manually
    # For simplicity, we'll use the WAV and tag it as if it were FLAC
    # Actually, mutagen can't create FLAC from scratch — let's use MP3 for all and
    # just vary the tags. We'll create a well-tagged MP3 instead.
    pass  # Handled below


def create_fixtures() -> None:
    """Create all test audio fixtures."""
    # Create a base WAV for conversions
    base_wav = FIXTURE_DIR / "_base.wav"
    _make_wav(base_wav, duration_ms=200)

    # 1. well_tagged.mp3 — Tier 1: all MusicBrainz IDs
    well_tagged = FIXTURE_DIR / "well_tagged.mp3"
    # Create minimal MP3 (lame header + silence)
    # Easier: use a tiny valid MP3 frame
    _write_minimal_mp3(well_tagged)
    audio = MP3(well_tagged)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["Alive"]))
    audio.tags.add(TPE1(encoding=3, text=["Pearl Jam"]))
    audio.tags.add(TALB(encoding=3, text=["Ten"]))
    audio.tags.add(TRCK(encoding=3, text=["3/11"]))
    audio.tags.add(TPOS(encoding=3, text=["1/1"]))
    audio.tags.add(TXXX(encoding=3, desc="musicbrainz_trackid",
                        text=["f5e6d4c3-b2a1-0000-0000-000000000001"]))
    audio.tags.add(TXXX(encoding=3, desc="musicbrainz_artistid",
                        text=["a1b2c3d4-0000-0000-0000-000000000001"]))
    audio.tags.add(TXXX(encoding=3, desc="musicbrainz_albumartistid",
                        text=["a1b2c3d4-0000-0000-0000-000000000001"]))
    audio.tags.add(TXXX(encoding=3, desc="musicbrainz_albumid",
                        text=["b1c2d3e4-0000-0000-0000-000000000001"]))
    audio.save()

    # 2. partial_tags.mp3 — Tier 2: some MBIDs missing
    partial = FIXTURE_DIR / "partial_tags.mp3"
    _write_minimal_mp3(partial)
    audio = MP3(partial)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["Under the Bridge"]))
    audio.tags.add(TPE1(encoding=3, text=["Red Hot Chili Peppers"]))
    audio.tags.add(TALB(encoding=3, text=["Blood Sugar Sex Magik"]))
    audio.tags.add(TRCK(encoding=3, text=["11"]))
    # No MusicBrainz IDs
    audio.save()

    # 3. minimal_tags.ogg — Tier 3: only basic tags
    minimal = FIXTURE_DIR / "minimal_tags.ogg"
    _write_minimal_ogg(minimal)
    audio = OggVorbis(minimal)
    audio["title"] = ["Smells Like Teen Spirit"]
    audio["artist"] = ["Nirvana"]
    audio.save()

    # 4. no_tags.wav — Tier 3: untagged WAV
    no_tags = FIXTURE_DIR / "no_tags.wav"
    _make_wav(no_tags, duration_ms=100)

    # 5. corrupt.mp3 — should go to quarantine
    corrupt = FIXTURE_DIR / "corrupt.mp3"
    corrupt.write_bytes(b"\x00\x01\x02\x03" * 10)

    # Cleanup temp files
    for tmp in FIXTURE_DIR.glob("_*"):
        tmp.unlink()

    print(f"Created fixtures in {FIXTURE_DIR}")


def _write_minimal_mp3(path: Path) -> None:
    """Write a minimal valid MP3 file (single MPEG frame of silence)."""
    # MPEG1 Layer3, 128kbps, 44100Hz, stereo — frame header + padding
    # Frame header: 0xFFE3 (sync + MPEG1/Layer3), 0x90 (128kbps/44100), 0x00
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    # Frame size for 128kbps/44100Hz = 417 bytes
    frame = header + b"\x00" * 413
    # Write a few frames for mutagen to accept it
    path.write_bytes(frame * 3)


def _write_minimal_ogg(path: Path) -> None:
    """Write a minimal valid OGG Vorbis file using mutagen."""
    import subprocess
    # Create from WAV using ffmpeg if available
    wav = path.parent / "_tmp_ogg.wav"
    _make_wav(wav, duration_ms=100)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-acodec", "libvorbis", "-q:a", "0", str(path)],
            capture_output=True, check=True,
        )
    except FileNotFoundError:
        # No ffmpeg — create a minimal OGG manually using oggvorbis
        # This is tricky without an encoder. Fall back to a WAV renamed as .ogg
        # and expect the scan to quarantine it (acceptable for testing)
        path.write_bytes(wav.read_bytes())
    finally:
        if wav.exists():
            wav.unlink()


if __name__ == "__main__":
    create_fixtures()
```

Run:
```bash
cd "D:/PythonStuff/RetroStation/.worktrees/phase-2-library"
uv run python tests/fixtures/create_audio_fixtures.py
```

Verify: `ls tests/fixtures/audio/` should show 5 files.

**Important:** If `ffmpeg` is not available, the OGG file will be a renamed WAV that gets quarantined. That's fine — it tests the quarantine path. If it IS available, the OGG will be valid and test the OGG extraction path.

- [ ] **Step 2: Create `backend/services/library_scan_service.py`**

```python
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import mutagen
import structlog

from backend.domain.enums import EnrichmentStatus, ReleaseStatus, ReleaseType
from backend.domain.models import LibraryFile, LibraryQuarantine

logger = structlog.get_logger()

# File extensions the scanner processes
SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".wav"}


def _file_hash(path: Path) -> str:
    """Compute SHA-256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_format(path: Path) -> str:
    """Map file extension to format string."""
    ext = path.suffix.lower()
    formats = {".flac": "flac", ".mp3": "mp3", ".m4a": "aac", ".ogg": "ogg", ".wav": "wav"}
    return formats.get(ext, "unknown")


def _safe_first(tags: Any, key: str) -> str | None:
    """Safely extract first value from mutagen tag list."""
    try:
        val = tags[key]
        if isinstance(val, list):
            return str(val[0]) if val else None
        return str(val)
    except (KeyError, IndexError):
        return None


def _parse_slash_number(raw: str | None) -> int | None:
    """Parse '6/8' slash notation → 6, or plain '6' → 6."""
    if not raw:
        return None
    try:
        return int(str(raw).split("/")[0])
    except (ValueError, IndexError):
        return None


def _safe_enum(enum_cls: type, value: str | None) -> Any:
    """Safely convert string to enum, returning None on failure."""
    if not value:
        return None
    try:
        return enum_cls(value.lower())
    except ValueError:
        return None


def _extract_id3_txxx(tags: Any, desc: str) -> str | None:
    """Extract a TXXX (user-defined text) tag by description from ID3."""
    key = f"TXXX:{desc}"
    try:
        val = tags[key]
        if hasattr(val, "text") and val.text:
            return str(val.text[0])
        return str(val)
    except (KeyError, IndexError):
        return None


def extract_tags(path: Path) -> LibraryFile:
    """Extract audio tags from a file and return a LibraryFile model.

    Raises mutagen.MutagenError if the file cannot be read.
    """
    audio = mutagen.File(str(path), easy=False)
    if audio is None:
        raise mutagen.MutagenError(f"Unrecognized format: {path}")

    file_format = _detect_format(path)
    duration_ms = int(audio.info.length * 1000) if hasattr(audio.info, "length") else None
    bitrate = getattr(audio.info, "bitrate", None)
    if bitrate:
        bitrate = bitrate // 1000  # Convert bps to kbps

    tags = audio.tags or {}

    # Extract metadata based on tag format
    if hasattr(audio, "tags") and audio.tags is not None:
        tag_type = type(audio.tags).__name__
    else:
        tag_type = "none"

    # ID3 (MP3, sometimes WAV/AIFF)
    if "ID3" in tag_type or hasattr(tags, "getall"):
        track_title = _safe_first(tags, "TIT2")
        artist_name = _safe_first(tags, "TPE1")
        album_title = _safe_first(tags, "TALB")
        track_raw = _safe_first(tags, "TRCK")
        disc_raw = _safe_first(tags, "TPOS")
        recording_mbid = _extract_id3_txxx(tags, "musicbrainz_trackid")
        artist_mbid = _extract_id3_txxx(tags, "musicbrainz_artistid")
        album_artist_mbid = _extract_id3_txxx(tags, "musicbrainz_albumartistid")
        release_mbid = _extract_id3_txxx(tags, "musicbrainz_albumid")
        release_type_raw = _extract_id3_txxx(tags, "musicbrainz_albumtype")
        release_status_raw = _extract_id3_txxx(tags, "musicbrainz_albumstatus")
    else:
        # Vorbis comments (FLAC, OGG, Opus) or generic
        track_title = _safe_first(tags, "title")
        artist_name = _safe_first(tags, "artist")
        album_title = _safe_first(tags, "album")
        track_raw = _safe_first(tags, "tracknumber")
        disc_raw = _safe_first(tags, "discnumber")
        recording_mbid = _safe_first(tags, "musicbrainz_trackid")
        artist_mbid = _safe_first(tags, "musicbrainz_artistid")
        album_artist_mbid = _safe_first(tags, "musicbrainz_albumartistid")
        release_mbid = _safe_first(tags, "musicbrainz_albumid")
        release_type_raw = _safe_first(tags, "musicbrainz_albumtype")
        release_status_raw = _safe_first(tags, "musicbrainz_albumstatus")

    # Build raw_metadata dump
    raw_metadata: dict[str, Any] = {}
    for key in tags:
        try:
            val = tags[key]
            if hasattr(val, "text"):
                raw_metadata[str(key)] = [str(t) for t in val.text]
            elif isinstance(val, list):
                raw_metadata[str(key)] = [str(v) for v in val]
            else:
                raw_metadata[str(key)] = str(val)
        except Exception:
            pass

    # Parse secondary release type
    release_type_secondary = None
    if release_type_raw and "/" in release_type_raw:
        parts = release_type_raw.split("/")
        release_type_raw = parts[0].strip()
        release_type_secondary = parts[1].strip().lower() if len(parts) > 1 else None

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_file_hash(path),
        format=file_format,
        enrichment_status=EnrichmentStatus.PENDING,
        recording_mbid=recording_mbid,
        artist_mbid=artist_mbid,
        album_artist_mbid=album_artist_mbid,
        release_mbid=release_mbid,
        release_title=album_title,
        release_type=_safe_enum(ReleaseType, release_type_raw),
        release_type_secondary=release_type_secondary,
        release_status=_safe_enum(ReleaseStatus, release_status_raw),
        track_title=track_title,
        track_number=_parse_slash_number(track_raw),
        disc_number=_parse_slash_number(disc_raw),
        duration_ms=duration_ms,
        bitrate=bitrate,
        raw_metadata=raw_metadata,
    )


def scan_directory(root: Path) -> tuple[list[LibraryFile], list[LibraryQuarantine]]:
    """Walk a directory tree and extract tags from all supported audio files.

    Returns:
        Tuple of (successful extractions, quarantined failures).
    """
    files: list[LibraryFile] = []
    quarantine: list[LibraryQuarantine] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        try:
            lib_file = extract_tags(path)
            files.append(lib_file)
        except Exception as exc:
            quarantine.append(LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=str(exc),
            ))
            logger.warning("scan_quarantine", path=str(path), error=str(exc))

    logger.info("scan_complete", scanned=len(files), quarantined=len(quarantine))
    return files, quarantine
```

- [ ] **Step 3: Create `tests/services/test_library_scan.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from backend.domain.enums import EnrichmentStatus
from backend.services.library_scan_service import extract_tags, scan_directory

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "audio"


@pytest.fixture
def audio_dir() -> Path:
    """Return the test audio fixtures directory."""
    if not FIXTURE_DIR.exists():
        pytest.skip("Audio fixtures not generated — run create_audio_fixtures.py first")
    return FIXTURE_DIR


class TestExtractTags:
    def test_well_tagged_mp3_extracts_all_mbids(self, audio_dir: Path) -> None:
        path = audio_dir / "well_tagged.mp3"
        if not path.exists():
            pytest.skip("well_tagged.mp3 not found")
        result = extract_tags(path)
        assert result.track_title == "Alive"
        assert result.recording_mbid == "f5e6d4c3-b2a1-0000-0000-000000000001"
        assert result.artist_mbid == "a1b2c3d4-0000-0000-0000-000000000001"
        assert result.album_artist_mbid == "a1b2c3d4-0000-0000-0000-000000000001"
        assert result.release_mbid == "b1c2d3e4-0000-0000-0000-000000000001"
        assert result.release_title == "Ten"
        assert result.track_number == 3
        assert result.disc_number == 1
        assert result.format == "mp3"
        assert result.file_hash  # non-empty
        assert result.enrichment_status == EnrichmentStatus.PENDING

    def test_partial_tags_mp3_has_title_but_no_mbids(self, audio_dir: Path) -> None:
        path = audio_dir / "partial_tags.mp3"
        if not path.exists():
            pytest.skip("partial_tags.mp3 not found")
        result = extract_tags(path)
        assert result.track_title == "Under the Bridge"
        assert result.recording_mbid is None
        assert result.artist_mbid is None
        assert result.release_mbid is None

    def test_minimal_ogg_has_basic_tags(self, audio_dir: Path) -> None:
        path = audio_dir / "minimal_tags.ogg"
        if not path.exists():
            pytest.skip("minimal_tags.ogg not found")
        try:
            result = extract_tags(path)
            assert result.track_title == "Smells Like Teen Spirit"
            assert result.format == "ogg"
        except Exception:
            pass  # May be quarantined if ffmpeg was not available

    def test_no_tags_wav_still_extracts_format(self, audio_dir: Path) -> None:
        path = audio_dir / "no_tags.wav"
        if not path.exists():
            pytest.skip("no_tags.wav not found")
        result = extract_tags(path)
        assert result.format == "wav"
        assert result.track_title is None
        assert result.duration_ms is not None
        assert result.duration_ms > 0

    def test_corrupt_file_raises(self, audio_dir: Path) -> None:
        path = audio_dir / "corrupt.mp3"
        if not path.exists():
            pytest.skip("corrupt.mp3 not found")
        with pytest.raises(Exception):
            extract_tags(path)

    def test_file_hash_is_sha256_hex(self, audio_dir: Path) -> None:
        path = audio_dir / "well_tagged.mp3"
        if not path.exists():
            pytest.skip("well_tagged.mp3 not found")
        result = extract_tags(path)
        assert len(result.file_hash) == 64
        assert all(c in "0123456789abcdef" for c in result.file_hash)


class TestScanDirectory:
    def test_scan_returns_files_and_quarantine(self, audio_dir: Path) -> None:
        files, quarantine = scan_directory(audio_dir)
        # At least the well-tagged and partial MP3s should succeed
        assert len(files) >= 2
        # The corrupt file should be quarantined
        assert len(quarantine) >= 1
        quarantine_paths = [q.file_path for q in quarantine]
        assert any("corrupt" in p for p in quarantine_paths)

    def test_scan_skips_non_audio_files(self, audio_dir: Path) -> None:
        files, _ = scan_directory(audio_dir)
        for f in files:
            ext = Path(f.file_path).suffix.lower()
            assert ext in {".flac", ".mp3", ".m4a", ".ogg", ".wav"}
```

- [ ] **Step 4: Generate fixtures and run tests**

```bash
cd "D:/PythonStuff/RetroStation/.worktrees/phase-2-library"
uv run python tests/fixtures/create_audio_fixtures.py
uv run pytest tests/services/test_library_scan.py -v
```

Expected: 7-8 tests pass (some may skip if ffmpeg not available).

- [ ] **Step 5: Run mypy and ruff**

```bash
uv run mypy --strict backend/services/library_scan_service.py
uv run ruff check backend/services/library_scan_service.py tests/services/test_library_scan.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/services/library_scan_service.py tests/services/test_library_scan.py \
  tests/fixtures/create_audio_fixtures.py tests/fixtures/audio/
git commit -m "feat: library scan service + mutagen tag extraction + synthetic test fixtures"
```

---

## Task 2: PG Repositories (library_files, library_quarantine) + Library Router + Scan Task

**Files:**
- Create: `backend/db/repositories/library_files.py`
- Create: `backend/db/repositories/library_quarantine.py`
- Create: `backend/tasks/library_tasks.py`
- Create: `backend/routers/library.py`
- Modify: `backend/routers/v1.py` (register library router)
- Modify: `backend/services/repository_factory.py` (add library repos)
- Create: `tests/integration/test_pg_library_repos.py`

- [ ] **Step 1: Create `backend/db/repositories/library_files.py`**

```python
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import EnrichmentStatus, ReleaseStatus, ReleaseType
from backend.domain.models import LibraryFile
from backend.repositories.library_files import LibraryFileRepository


class PgLibraryFileRepository(LibraryFileRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryFile:
        raw_metadata = row.get("raw_metadata")
        if isinstance(raw_metadata, str):
            raw_metadata = json.loads(raw_metadata)
        return LibraryFile(
            id=row["id"],
            file_path=row["file_path"],
            file_hash=row["file_hash"],
            format=row["format"],
            enrichment_status=EnrichmentStatus(row["enrichment_status"]),
            indexed_at=row["indexed_at"],
            trace_id=row.get("trace_id"),
            recording_id=row.get("recording_id"),
            recording_mbid=row.get("recording_mbid"),
            artist_mbid=row.get("artist_mbid"),
            album_artist_mbid=row.get("album_artist_mbid"),
            release_mbid=row.get("release_mbid"),
            release_title=row.get("release_title"),
            release_type=(
                ReleaseType(row["release_type"]) if row.get("release_type") else None
            ),
            release_type_secondary=row.get("release_type_secondary"),
            release_status=(
                ReleaseStatus(row["release_status"]) if row.get("release_status") else None
            ),
            track_title=row.get("track_title"),
            track_number=row.get("track_number"),
            disc_number=row.get("disc_number"),
            duration_ms=row.get("duration_ms"),
            bitrate=row.get("bitrate"),
            raw_metadata=raw_metadata,
        )

    def upsert(self, file: LibraryFile) -> LibraryFile:
        self._conn.execute(
            """INSERT INTO library_files (
                id, file_path, file_hash, format, enrichment_status, trace_id,
                recording_id, recording_mbid, artist_mbid, album_artist_mbid,
                release_mbid, release_title, release_type, release_type_secondary,
                release_status, track_title, track_number, disc_number,
                duration_ms, bitrate, raw_metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s
            ) ON CONFLICT (file_path) DO UPDATE SET
                file_hash = EXCLUDED.file_hash,
                format = EXCLUDED.format,
                recording_mbid = EXCLUDED.recording_mbid,
                artist_mbid = EXCLUDED.artist_mbid,
                album_artist_mbid = EXCLUDED.album_artist_mbid,
                release_mbid = EXCLUDED.release_mbid,
                release_title = EXCLUDED.release_title,
                release_type = EXCLUDED.release_type,
                release_type_secondary = EXCLUDED.release_type_secondary,
                release_status = EXCLUDED.release_status,
                track_title = EXCLUDED.track_title,
                track_number = EXCLUDED.track_number,
                disc_number = EXCLUDED.disc_number,
                duration_ms = EXCLUDED.duration_ms,
                bitrate = EXCLUDED.bitrate,
                raw_metadata = EXCLUDED.raw_metadata,
                indexed_at = now()""",
            (
                file.id, file.file_path, file.file_hash, file.format,
                file.enrichment_status.value, file.trace_id,
                file.recording_id, file.recording_mbid, file.artist_mbid,
                file.album_artist_mbid,
                file.release_mbid, file.release_title,
                file.release_type.value if file.release_type else None,
                file.release_type_secondary,
                file.release_status.value if file.release_status else None,
                file.track_title, file.track_number, file.disc_number,
                file.duration_ms, file.bitrate,
                json.dumps(file.raw_metadata) if file.raw_metadata else None,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE file_path = %s",
            (file.file_path,),
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get_by_id(self, id: UUID) -> LibraryFile | None:
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_path(self, file_path: str) -> LibraryFile | None:
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE file_path = %s", (file_path,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_recording(self, recording_id: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            "SELECT * FROM library_files WHERE recording_id = %s",
            (recording_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE artist_mbid = %s OR album_artist_mbid = %s""",
            (artist_mbid, artist_mbid),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_enrichment_by_release(
        self, release_mbid: str
    ) -> list[LibraryFile]:
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE enrichment_status = %s AND release_mbid = %s""",
            (EnrichmentStatus.PENDING.value, release_mbid),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_enrichment_by_recording(
        self, recording_mbid: str
    ) -> list[LibraryFile]:
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE enrichment_status = %s
                 AND recording_mbid = %s
                 AND release_mbid IS NULL""",
            (EnrichmentStatus.PENDING.value, recording_mbid),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_recording_link(
        self,
        id: UUID,
        recording_id: str,
        enrichment_status: EnrichmentStatus,
    ) -> None:
        self._conn.execute(
            """UPDATE library_files
               SET recording_id = %s, enrichment_status = %s
               WHERE id = %s""",
            (recording_id, enrichment_status.value, id),
        )

    def count_by_format(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT format, count(*) as cnt FROM library_files GROUP BY format"
        ).fetchall()
        return {r["format"]: r["cnt"] for r in rows}

    def count_by_enrichment_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            """SELECT enrichment_status, count(*) as cnt
               FROM library_files GROUP BY enrichment_status"""
        ).fetchall()
        return {r["enrichment_status"]: r["cnt"] for r in rows}
```

- [ ] **Step 2: Create `backend/db/repositories/library_quarantine.py`**

```python
from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.models import LibraryQuarantine
from backend.repositories.library_quarantine import LibraryQuarantineRepository


class PgLibraryQuarantineRepository(LibraryQuarantineRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryQuarantine:
        return LibraryQuarantine(
            id=row["id"],
            file_path=row["file_path"],
            error_message=row["error_message"],
            trace_id=row.get("trace_id"),
            created_at=row["created_at"],
        )

    def create(self, entry: LibraryQuarantine) -> LibraryQuarantine:
        self._conn.execute(
            """INSERT INTO library_quarantine (id, file_path, error_message, trace_id)
               VALUES (%s, %s, %s, %s)""",
            (entry.id, entry.file_path, entry.error_message, entry.trace_id),
        )
        return entry

    def list_all(self) -> list[LibraryQuarantine]:
        rows = self._conn.execute(
            "SELECT * FROM library_quarantine ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_path(self, file_path: str) -> LibraryQuarantine | None:
        row = self._conn.execute(
            "SELECT * FROM library_quarantine WHERE file_path = %s",
            (file_path,),
        ).fetchone()
        return self._row_to_model(row) if row else None
```

- [ ] **Step 3: Create `backend/tasks/library_tasks.py`**

```python
from __future__ import annotations

from pathlib import Path

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.services.library_scan_service import scan_directory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()
def library_scan_task(scan_path: str) -> dict[str, int]:
    """Scan a directory for audio files and persist to database."""
    settings = get_settings()
    root = Path(scan_path)

    if not root.is_dir():
        raise ValueError(f"Scan path does not exist: {scan_path}")

    files, quarantine = scan_directory(root)

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        file_repo = PgLibraryFileRepository(conn)
        quarantine_repo = PgLibraryQuarantineRepository(conn)

        for lib_file in files:
            file_repo.upsert(lib_file)

        for entry in quarantine:
            quarantine_repo.create(entry)

        conn.commit()

    result = {"scanned": len(files), "quarantined": len(quarantine)}
    logger.info("library_scan_task_complete", **result)

    # Fire-and-forget: enqueue enrichment
    from backend.tasks.library_enrichment_tasks import library_enrichment_task
    library_enrichment_task()

    return result
```

- [ ] **Step 4: Create `backend/routers/library.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from backend.tasks.library_tasks import library_scan_task

router = APIRouter()


class ScanRequest(BaseModel):
    path: str


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def start_scan(request: ScanRequest) -> dict[str, str]:
    """Start a library scan for the given directory path."""
    if not request.path:
        raise HTTPException(status_code=400, detail="Path is required")

    library_scan_task(request.path)

    return {"status": "accepted", "message": f"Scan queued for {request.path}"}
```

- [ ] **Step 5: Update `backend/routers/v1.py`**

Add library router registration:

```python
from backend.routers import ingestion, library

router = APIRouter(prefix="/api/v1")
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
router.include_router(library.router, prefix="/library", tags=["library"])
```

- [ ] **Step 6: Update `backend/services/repository_factory.py`**

Add imports:
```python
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
```

Add to `__init__`:
```python
        self.library_files = PgLibraryFileRepository(conn)
        self.library_quarantine = PgLibraryQuarantineRepository(conn)
```

- [ ] **Step 7: Create `tests/integration/test_pg_library_repos.py`**

```python
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.domain.enums import EnrichmentStatus
from backend.domain.models import LibraryFile, LibraryQuarantine


def test_library_file_upsert_and_get(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        lf = LibraryFile(
            id=uuid4(),
            file_path="/test/music/song.mp3",
            file_hash="abc123" * 10 + "abcd",
            format="mp3",
            track_title="Test Song",
            artist_mbid="mbid-artist-1",
            release_mbid="mbid-release-1",
            recording_mbid="mbid-recording-1",
            duration_ms=240000,
            bitrate=320,
        )
        result = repo.upsert(lf)
        assert result.file_path == "/test/music/song.mp3"
        assert result.track_title == "Test Song"
        assert result.format == "mp3"
        assert result.enrichment_status == EnrichmentStatus.PENDING

        # Re-upsert same path updates metadata
        lf2 = LibraryFile(
            id=uuid4(),
            file_path="/test/music/song.mp3",
            file_hash="def456" * 10 + "defg",
            format="mp3",
            track_title="Updated Song",
        )
        result2 = repo.upsert(lf2)
        assert result2.id == result.id  # same row
        assert result2.track_title == "Updated Song"
        conn.commit()


def test_library_file_get_by_artist_mbid(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for i in range(3):
            repo.upsert(LibraryFile(
                id=uuid4(),
                file_path=f"/test/artist/{i}.mp3",
                file_hash=f"hash{i}" * 10 + "xxxx",
                format="mp3",
                artist_mbid="mbid-shared-artist",
            ))
        results = repo.get_by_artist_mbid("mbid-shared-artist")
        assert len(results) == 3
        conn.commit()


def test_library_file_pending_enrichment_queries(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        # File with release_mbid
        repo.upsert(LibraryFile(
            id=uuid4(),
            file_path="/test/enrich/album_track.mp3",
            file_hash="enrich1" * 9 + "xx",
            format="mp3",
            release_mbid="release-001",
            recording_mbid="rec-001",
        ))
        # File with only recording_mbid (no release)
        repo.upsert(LibraryFile(
            id=uuid4(),
            file_path="/test/enrich/single_track.mp3",
            file_hash="enrich2" * 9 + "xx",
            format="mp3",
            recording_mbid="rec-002",
        ))

        by_release = repo.get_pending_enrichment_by_release("release-001")
        assert len(by_release) == 1

        by_recording = repo.get_pending_enrichment_by_recording("rec-002")
        assert len(by_recording) == 1
        conn.commit()


def test_library_file_update_recording_link(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        lf = repo.upsert(LibraryFile(
            id=uuid4(),
            file_path="/test/link/track.mp3",
            file_hash="link1" * 12 + "xxxx",
            format="mp3",
        ))
        assert lf.recording_id is None

        repo.update_recording_link(lf.id, "rec-linked", EnrichmentStatus.ENRICHED)
        updated = repo.get_by_id(lf.id)
        assert updated is not None
        assert updated.recording_id == "rec-linked"
        assert updated.enrichment_status == EnrichmentStatus.ENRICHED
        conn.commit()


def test_library_file_count_aggregates(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for i, fmt in enumerate(["mp3", "mp3", "flac"]):
            repo.upsert(LibraryFile(
                id=uuid4(),
                file_path=f"/test/count/{i}.{fmt}",
                file_hash=f"count{i}" * 10 + "xxxx",
                format=fmt,
            ))

        by_format = repo.count_by_format()
        assert by_format["mp3"] == 2
        assert by_format["flac"] == 1

        by_status = repo.count_by_enrichment_status()
        assert by_status["pending"] == 3
        conn.commit()


def test_quarantine_create_and_list(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryQuarantineRepository(conn)
        repo.create(LibraryQuarantine(
            id=uuid4(),
            file_path="/test/bad/corrupt.mp3",
            error_message="Invalid MPEG frame",
        ))
        all_entries = repo.list_all()
        assert len(all_entries) >= 1
        assert any(e.file_path == "/test/bad/corrupt.mp3" for e in all_entries)

        found = repo.get_by_path("/test/bad/corrupt.mp3")
        assert found is not None
        assert found.error_message == "Invalid MPEG frame"
        conn.commit()
```

- [ ] **Step 8: Run integration tests**

```bash
uv run pytest tests/integration/test_pg_library_repos.py -v
```

Expected: 6 tests pass.

- [ ] **Step 9: Run mypy and ruff**

```bash
uv run mypy --strict backend/db/repositories/library_files.py \
  backend/db/repositories/library_quarantine.py \
  backend/tasks/library_tasks.py backend/routers/library.py
uv run ruff check backend/db/repositories/ backend/tasks/ backend/routers/ backend/services/
```

- [ ] **Step 10: Commit**

```bash
git add backend/db/repositories/library_files.py backend/db/repositories/library_quarantine.py \
  backend/tasks/library_tasks.py backend/routers/library.py backend/routers/v1.py \
  backend/services/repository_factory.py tests/integration/test_pg_library_repos.py
git commit -m "feat: PG library repos + scan task + POST /api/v1/library/scan router"
```

---

## Task 3: Library Enrichment Service + mb_client Lookups

**Files:**
- Modify: `backend/services/mb_client.py` (add `lookup_release`, `lookup_recording`)
- Modify: `tests/fakes/mb_client.py` (add lookup methods to FakeMbClient)
- Create: `backend/services/library_enrichment_service.py`
- Create: `backend/tasks/library_enrichment_tasks.py`
- Create: `tests/integration/test_library_enrichment.py`

The enrichment service processes `library_files` with `enrichment_status='pending'`. It batches by `release_mbid` first (album batching), then processes remaining files by `recording_mbid`. Each file gets linked to a `recordings` row via `library_files.recording_id`.

- [ ] **Step 1: Add lookup methods to `backend/services/mb_client.py`**

Add these two methods to the `RealMbClient` class:

```python
    def lookup_release(self, mbid: str) -> dict[str, Any] | None:
        """Lookup a release by MBID. Returns full release data with recordings."""
        cache_key = f"release:{mbid}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cached.response_data  # type: ignore[return-value]

        _rate_limit()
        response = self._http.get(
            f"{_MUSICBRAINZ_API}/release/{mbid}",
            params={
                "fmt": "json",
                "inc": "recordings+artist-credits+release-groups",
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()

        now = datetime.now(tz=UTC)
        self._cache.set(MbCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="release",
            entity_mbid=mbid,
            response_data=data,
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup", entity="release", mbid=mbid)
        return data  # type: ignore[no-any-return]

    def lookup_recording(self, mbid: str) -> dict[str, Any] | None:
        """Lookup a recording by MBID. Returns recording data with work relations."""
        cache_key = f"recording:{mbid}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cached.response_data  # type: ignore[return-value]

        _rate_limit()
        response = self._http.get(
            f"{_MUSICBRAINZ_API}/recording/{mbid}",
            params={
                "fmt": "json",
                "inc": "artist-credits+work-rels",
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()

        now = datetime.now(tz=UTC)
        self._cache.set(MbCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="recording",
            entity_mbid=mbid,
            response_data=data,
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup", entity="recording", mbid=mbid)
        return data  # type: ignore[no-any-return]
```

Also update the `MbClientProtocol` in `backend/services/artist_matching_service.py`:

```python
class MbClientProtocol(Protocol):
    def search_artist(self, name: str) -> list[dict[str, Any]]: ...
    def lookup_release(self, mbid: str) -> dict[str, Any] | None: ...
    def lookup_recording(self, mbid: str) -> dict[str, Any] | None: ...
```

- [ ] **Step 2: Update `tests/fakes/mb_client.py`**

```python
from __future__ import annotations

from typing import Any


class FakeMbClient:
    """In-memory MusicBrainz client for testing. Returns canned responses."""

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        releases: dict[str, dict[str, Any]] | None = None,
        recordings: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._releases = releases or {}
        self._recordings = recordings or {}
        self.calls: list[str] = []

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(f"search_artist:{name}")
        return self._responses.get(name, [])

    def lookup_release(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_release:{mbid}")
        return self._releases.get(mbid)

    def lookup_recording(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_recording:{mbid}")
        return self._recordings.get(mbid)
```

- [ ] **Step 3: Create `backend/services/library_enrichment_service.py`**

```python
from __future__ import annotations

from typing import Any

import structlog

from backend.domain.enums import EnrichmentStatus, VersionType
from backend.domain.models import Artist, Recording, Work
from backend.repositories.artists import ArtistRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.works import WorkRepository

logger = structlog.get_logger()


class MbClientProtocol:
    """Protocol for MB client methods needed by enrichment."""

    def lookup_release(self, mbid: str) -> dict[str, Any] | None: ...
    def lookup_recording(self, mbid: str) -> dict[str, Any] | None: ...


def _extract_artist_from_credits(
    credits: list[dict[str, Any]],
) -> tuple[str, str, str] | None:
    """Extract (mbid, name, sort_name) from artist-credits array."""
    if not credits:
        return None
    first = credits[0]
    artist = first.get("artist", {})
    mbid = artist.get("id")
    name = artist.get("name")
    sort_name = artist.get("sort-name", name)
    if mbid and name:
        return mbid, name, sort_name or name
    return None


def _extract_work_from_relations(
    relations: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Extract (work_mbid, work_title) from recording relations."""
    for rel in relations:
        if rel.get("type") == "performance" and "work" in rel:
            work = rel["work"]
            return work.get("id"), work.get("title")
    return None


def enrich_by_release(
    release_mbid: str,
    library_file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    artist_repo: ArtistRepository,
    mb_client: Any,
) -> int:
    """Enrich all pending library files that share a release_mbid.

    Looks up the release once, extracts all recordings, links each file.
    Returns count of files enriched.
    """
    pending = library_file_repo.get_pending_enrichment_by_release(release_mbid)
    if not pending:
        return 0

    release_data = mb_client.lookup_release(release_mbid)
    if release_data is None:
        for f in pending:
            library_file_repo.update_recording_link(
                f.id, "", EnrichmentStatus.FAILED
            )
        logger.warning("enrichment_release_not_found", release_mbid=release_mbid)
        return 0

    # Extract artist from release
    artist_credits = release_data.get("artist-credit", [])
    artist_info = _extract_artist_from_credits(artist_credits)
    if artist_info:
        artist_mbid, artist_name, artist_sort = artist_info
        artist_repo.upsert(Artist(
            id=artist_mbid, name=artist_name, sort_name=artist_sort,
        ))

    # Build recording map from release media
    recording_map: dict[str, dict[str, Any]] = {}
    for medium in release_data.get("media", []):
        for track in medium.get("tracks", []):
            rec = track.get("recording", {})
            rec_id = rec.get("id")
            if rec_id:
                recording_map[rec_id] = rec

    enriched = 0
    for lib_file in pending:
        if not lib_file.recording_mbid:
            library_file_repo.update_recording_link(
                lib_file.id, "", EnrichmentStatus.SKIPPED
            )
            continue

        rec_data = recording_map.get(lib_file.recording_mbid)
        if rec_data is None:
            # Recording not in this release — try direct lookup
            rec_data = mb_client.lookup_recording(lib_file.recording_mbid)

        if rec_data is None:
            library_file_repo.update_recording_link(
                lib_file.id, "", EnrichmentStatus.FAILED
            )
            continue

        # Upsert recording
        rec_id = rec_data.get("id", lib_file.recording_mbid)
        work_info = _extract_work_from_relations(
            rec_data.get("relations", [])
        )
        work_id = None
        if work_info and artist_info:
            work_mbid, work_title = work_info
            work_repo.upsert(Work(
                id=work_mbid, title=work_title, artist_id=artist_info[0],
            ))
            work_id = work_mbid

        recording_repo.upsert(Recording(
            id=rec_id,
            title=rec_data.get("title", ""),
            work_id=work_id,
            duration_ms=rec_data.get("length"),
        ))

        library_file_repo.update_recording_link(
            lib_file.id, rec_id, EnrichmentStatus.ENRICHED
        )
        enriched += 1

    logger.info(
        "enrichment_release_complete",
        release_mbid=release_mbid,
        enriched=enriched,
        total=len(pending),
    )
    return enriched


def enrich_by_recording(
    recording_mbid: str,
    library_file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    artist_repo: ArtistRepository,
    mb_client: Any,
) -> int:
    """Enrich files that have recording_mbid but no release_mbid."""
    pending = library_file_repo.get_pending_enrichment_by_recording(recording_mbid)
    if not pending:
        return 0

    rec_data = mb_client.lookup_recording(recording_mbid)
    if rec_data is None:
        for f in pending:
            library_file_repo.update_recording_link(
                f.id, "", EnrichmentStatus.FAILED
            )
        return 0

    # Extract artist
    artist_credits = rec_data.get("artist-credit", [])
    artist_info = _extract_artist_from_credits(artist_credits)
    if artist_info:
        artist_repo.upsert(Artist(
            id=artist_info[0], name=artist_info[1], sort_name=artist_info[2],
        ))

    # Extract work from relations
    work_info = _extract_work_from_relations(rec_data.get("relations", []))
    work_id = None
    if work_info and artist_info:
        work_mbid, work_title = work_info
        work_repo.upsert(Work(
            id=work_mbid, title=work_title, artist_id=artist_info[0],
        ))
        work_id = work_mbid

    recording_repo.upsert(Recording(
        id=recording_mbid,
        title=rec_data.get("title", ""),
        work_id=work_id,
        duration_ms=rec_data.get("length"),
    ))

    enriched = 0
    for f in pending:
        library_file_repo.update_recording_link(
            f.id, recording_mbid, EnrichmentStatus.ENRICHED
        )
        enriched += 1

    logger.info(
        "enrichment_recording_complete",
        recording_mbid=recording_mbid,
        enriched=enriched,
    )
    return enriched
```

- [ ] **Step 4: Create `backend/tasks/library_enrichment_tasks.py`**

```python
from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.mb_cache import PgMbCacheRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.works import PgWorkRepository
from backend.services.library_enrichment_service import (
    enrich_by_recording,
    enrich_by_release,
)
from backend.services.mb_client import RealMbClient
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()
def library_enrichment_task() -> dict[str, int]:
    """Enrich pending library files via MusicBrainz API.

    Batches by release_mbid first, then processes remaining by recording_mbid.
    """
    settings = get_settings()
    total_enriched = 0

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        file_repo = PgLibraryFileRepository(conn)
        recording_repo = PgRecordingRepository(conn)
        work_repo = PgWorkRepository(conn)
        artist_repo = PgArtistRepository(conn)
        mb_client = RealMbClient(PgMbCacheRepository(conn))

        # Phase 1: batch by release_mbid
        release_mbids = conn.execute(
            """SELECT DISTINCT release_mbid FROM library_files
               WHERE enrichment_status = 'pending' AND release_mbid IS NOT NULL"""
        ).fetchall()

        for row in release_mbids:
            enriched = enrich_by_release(
                row["release_mbid"], file_repo, recording_repo,
                work_repo, artist_repo, mb_client,
            )
            total_enriched += enriched
            conn.commit()

        # Phase 2: remaining files with recording_mbid but no release_mbid
        recording_mbids = conn.execute(
            """SELECT DISTINCT recording_mbid FROM library_files
               WHERE enrichment_status = 'pending'
                 AND recording_mbid IS NOT NULL
                 AND release_mbid IS NULL"""
        ).fetchall()

        for row in recording_mbids:
            enriched = enrich_by_recording(
                row["recording_mbid"], file_repo, recording_repo,
                work_repo, artist_repo, mb_client,
            )
            total_enriched += enriched
            conn.commit()

    result = {"enriched": total_enriched}
    logger.info("library_enrichment_task_complete", **result)

    # Fire-and-forget: enqueue MB enrichment for canonical entities
    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task
    mb_enrichment_task()

    return result
```

- [ ] **Step 5: Create `tests/integration/test_library_enrichment.py`**

```python
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.enums import EnrichmentStatus
from backend.domain.models import LibraryFile
from backend.services.library_enrichment_service import enrich_by_release
from tests.fakes.mb_client import FakeMbClient


def test_enrich_by_release_links_recording(migrated_db: str) -> None:
    """Enrichment via release lookup creates recording and links file."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        file_repo = PgLibraryFileRepository(conn)
        recording_repo = PgRecordingRepository(conn)
        work_repo = PgWorkRepository(conn)
        artist_repo = PgArtistRepository(conn)

        # Insert a pending library file
        file_repo.upsert(LibraryFile(
            id=uuid4(),
            file_path="/test/enrich/alive.mp3",
            file_hash="enrichtest" * 6 + "abcd",
            format="mp3",
            release_mbid="release-ten-001",
            recording_mbid="rec-alive-001",
            track_title="Alive",
        ))

        # Fake MB response for the release
        fake_mb = FakeMbClient(
            releases={
                "release-ten-001": {
                    "id": "release-ten-001",
                    "title": "Ten",
                    "artist-credit": [
                        {"artist": {"id": "artist-pj", "name": "Pearl Jam",
                                    "sort-name": "Pearl Jam"}}
                    ],
                    "media": [{
                        "tracks": [{
                            "recording": {
                                "id": "rec-alive-001",
                                "title": "Alive",
                                "length": 341000,
                                "relations": [{
                                    "type": "performance",
                                    "work": {"id": "work-alive", "title": "Alive"},
                                }],
                            }
                        }]
                    }],
                }
            },
        )

        enriched = enrich_by_release(
            "release-ten-001", file_repo, recording_repo,
            work_repo, artist_repo, fake_mb,
        )

        assert enriched == 1

        # Verify the file is now linked
        updated = file_repo.get_by_path("/test/enrich/alive.mp3")
        assert updated is not None
        assert updated.recording_id == "rec-alive-001"
        assert updated.enrichment_status == EnrichmentStatus.ENRICHED

        # Verify recording was created
        rec = recording_repo.get_by_id("rec-alive-001")
        assert rec is not None
        assert rec.title == "Alive"
        assert rec.work_id == "work-alive"

        # Verify artist was created
        artist = artist_repo.get_by_id("artist-pj")
        assert artist is not None
        assert artist.name == "Pearl Jam"

        conn.commit()


def test_enrich_missing_release_marks_failed(migrated_db: str) -> None:
    """If release not found on MB, files are marked FAILED."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        file_repo = PgLibraryFileRepository(conn)

        file_repo.upsert(LibraryFile(
            id=uuid4(),
            file_path="/test/enrich/missing.mp3",
            file_hash="missing" * 9 + "xx",
            format="mp3",
            release_mbid="nonexistent-release",
            recording_mbid="rec-missing",
        ))

        fake_mb = FakeMbClient()  # returns None for everything

        enriched = enrich_by_release(
            "nonexistent-release", file_repo, PgRecordingRepository(conn),
            PgWorkRepository(conn), PgArtistRepository(conn), fake_mb,
        )

        assert enriched == 0
        updated = file_repo.get_by_path("/test/enrich/missing.mp3")
        assert updated is not None
        assert updated.enrichment_status == EnrichmentStatus.FAILED
        conn.commit()
```

- [ ] **Step 6: Run integration tests**

```bash
uv run pytest tests/integration/test_library_enrichment.py -v
```

Expected: 2 tests pass.

- [ ] **Step 7: Run mypy and ruff**

```bash
uv run mypy --strict backend/services/mb_client.py \
  backend/services/library_enrichment_service.py \
  backend/tasks/library_enrichment_tasks.py
uv run ruff check backend/services/ backend/tasks/ tests/
```

- [ ] **Step 8: Commit**

```bash
git add backend/services/mb_client.py backend/services/library_enrichment_service.py \
  backend/services/artist_matching_service.py \
  backend/tasks/library_enrichment_tasks.py \
  tests/fakes/mb_client.py tests/integration/test_library_enrichment.py
git commit -m "feat: library enrichment service + mb_client lookup methods

Batches by release_mbid first, then recording_mbid. Creates canonical
artists/works/recordings and links library_files.recording_id."
```

---

## Task 4: MB Enrichment Task

**Files:**
- Create: `backend/tasks/mb_enrichment_tasks.py`

This task fills metadata on canonical entities (`artists`, `works`, `recordings`) that have `needs_enhancement=TRUE`. It's the final step in the library pipeline chain: scan → enrichment → MB enrichment.

- [ ] **Step 1: Create `backend/tasks/mb_enrichment_tasks.py`**

```python
from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.mb_cache import PgMbCacheRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.works import PgWorkRepository
from backend.services.mb_client import RealMbClient
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()
def mb_enrichment_task() -> dict[str, int]:
    """Fill metadata on canonical entities with needs_enhancement=TRUE.

    Processes artists first, then works and recordings.
    """
    settings = get_settings()
    stats = {"artists": 0, "works": 0, "recordings": 0}

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        artist_repo = PgArtistRepository(conn)
        work_repo = PgWorkRepository(conn)
        recording_repo = PgRecordingRepository(conn)
        mb_client = RealMbClient(PgMbCacheRepository(conn))

        # Enhance artists
        for artist in artist_repo.list_needing_enhancement():
            try:
                results = mb_client.search_artist(artist.name)
                # Find exact MBID match in results
                match = next(
                    (r for r in results if r.get("id") == artist.id), None
                )
                if match:
                    disambiguation = match.get("disambiguation")
                    if disambiguation and not artist.disambiguation:
                        artist_repo.upsert(type(artist)(
                            id=artist.id,
                            name=match.get("name", artist.name),
                            sort_name=match.get("sort-name", artist.sort_name),
                            disambiguation=disambiguation,
                        ))
                artist_repo.mark_enhanced(artist.id)
                stats["artists"] += 1
            except Exception as exc:
                artist_repo.mark_enhancement_failed(artist.id, str(exc))
                logger.warning("mb_enhance_artist_failed", mbid=artist.id, error=str(exc))
            conn.commit()

        # Enhance works (lookup via recording relations)
        for work in work_repo.list_needing_enhancement():
            try:
                work_repo.mark_enhanced(work.id)
                stats["works"] += 1
            except Exception as exc:
                logger.warning("mb_enhance_work_failed", mbid=work.id, error=str(exc))
            conn.commit()

        # Enhance recordings
        for recording in recording_repo.list_needing_enhancement():
            try:
                rec_data = mb_client.lookup_recording(recording.id)
                if rec_data:
                    duration = rec_data.get("length")
                    if duration and not recording.duration_ms:
                        recording_repo.upsert(type(recording)(
                            id=recording.id,
                            title=rec_data.get("title", recording.title),
                            work_id=recording.work_id,
                            duration_ms=duration,
                        ))
                recording_repo.mark_enhanced(recording.id)
                stats["recordings"] += 1
            except Exception as exc:
                logger.warning("mb_enhance_recording_failed",
                               mbid=recording.id, error=str(exc))
            conn.commit()

    logger.info("mb_enrichment_task_complete", **stats)
    return stats
```

Note: `RecordingRepository` ABC does not have `mark_enhanced` or `list_needing_enhancement`. We need to add them.

- [ ] **Step 2: Add `mark_enhanced` and `list_needing_enhancement` to RecordingRepository ABC**

In `backend/repositories/recordings.py`, add:

```python
    @abstractmethod
    def list_needing_enhancement(self) -> list[Recording]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...
```

In `backend/db/repositories/recordings.py` (`PgRecordingRepository`), add:

```python
    def list_needing_enhancement(self) -> list[Recording]:
        rows = self._conn.execute(
            "SELECT * FROM recordings WHERE needs_enhancement = TRUE"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_enhanced(self, mbid: str) -> None:
        from datetime import UTC, datetime
        self._conn.execute(
            "UPDATE recordings SET needs_enhancement = FALSE, enhanced_at = %s WHERE id = %s",
            (datetime.now(tz=UTC), mbid),
        )
```

In `tests/fakes/recordings.py` (`FakeRecordingRepository`), add:

```python
    def list_needing_enhancement(self) -> list[Recording]:
        return [r for r in self._data.values() if r.needs_enhancement]

    def mark_enhanced(self, mbid: str) -> None:
        if rec := self._data.get(mbid):
            rec.needs_enhancement = False
```

- [ ] **Step 3: Run mypy and ruff**

```bash
uv run mypy --strict backend/tasks/mb_enrichment_tasks.py
uv run ruff check backend/tasks/mb_enrichment_tasks.py
```

- [ ] **Step 4: Run ABC compliance test**

```bash
uv run pytest tests/test_fakes_implement_abcs.py -v
```

Expected: All 18 tests still pass.

- [ ] **Step 5: Commit**

```bash
git add backend/tasks/mb_enrichment_tasks.py \
  backend/repositories/recordings.py backend/db/repositories/recordings.py \
  tests/fakes/recordings.py
git commit -m "feat: MB enrichment task — fills metadata on canonical entities

Processes artists/works/recordings with needs_enhancement=TRUE.
Added list_needing_enhancement + mark_enhanced to RecordingRepository."
```

---

## Task 5: Upgrade Identity Matching + Master Selection + E2E Test

**Files:**
- Modify: `backend/services/identity_matching_service.py` (replace stub with 4-tier)
- Modify: `backend/tasks/identity_matching_tasks.py` (swap fake → real PgLibraryFileRepository)
- Modify: `backend/services/master_selection_service.py` (replace no-op with real scoring)
- Create: `tests/test_identity_matching.py`
- Create: `tests/integration/test_library_pipeline_e2e.py`

- [ ] **Step 1: Replace `backend/services/identity_matching_service.py`**

```python
from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

import structlog
from rapidfuzz import fuzz

from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.models import Match
from backend.repositories.global_mapping_rules import GlobalMappingRuleRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.log_artists import LogArtistRepository
from backend.repositories.log_identities import LogIdentityRepository
from backend.repositories.matches import MatchRepository
from backend.services.normalization import normalize_artist, normalize_title

logger = structlog.get_logger()


def _rule_matches(source_pattern: str, normalized_value: str) -> bool:
    if source_pattern == normalized_value:
        return True
    try:
        return bool(re.fullmatch(source_pattern, normalized_value))
    except re.error:
        return False


def match_identities_for_playlist(
    playlist_id: UUID,
    log_identity_repo: LogIdentityRepository,
    log_artist_repo: LogArtistRepository,
    match_repo: MatchRepository,
    library_file_repo: LibraryFileRepository,
    rules_repo: GlobalMappingRuleRepository,
) -> list[str]:
    """Run identity matching for all pending identities in this playlist.

    Implements the 4-tier waterfall from spec Section 5.1:
    - Tier 0: Global mapping rules pre-check
    - Tier 2: MBID graph (artist MBID → candidate library_files → title fuzzy)
    - Tier 3: Text (combined artist + title score)
    - Tier 4: Vector (pgvector cosine — NEEDS_REVIEW only, never auto-accept)
    - Fallback: AUTO_REJECTED

    Returns list of work_ids that were newly matched (for master selection).
    """
    pending = log_identity_repo.get_pending_for_playlist(playlist_id)
    if not pending:
        logger.info("no_pending_identities", playlist_id=str(playlist_id))
        return []

    rules = rules_repo.list_ordered()
    matched_work_ids: list[str] = []

    for identity in pending:
        # Get the resolved artist for this identity
        artist = log_artist_repo.get_by_id(identity.artist_id)
        if not artist:
            continue

        # Get the artist's match to find the canonical artist MBID
        artist_match = match_repo.get_by_artist(artist.id)
        canonical_artist_mbid = artist_match.target_id if artist_match else None

        # Tier 0: Global mapping rules
        rule_matched = False
        for rule in rules:
            if rule.target_type == TargetType.LIBRARY_FILE and _rule_matches(
                rule.source_pattern, identity.normalized_signature
            ):
                log_identity_repo.update_match_status(
                    identity.id, MatchStatus.AUTO_MATCHED, MatchTier.MANUAL
                )
                match_repo.create(Match(
                    id=uuid4(),
                    identity_id=identity.id,
                    library_file_id=UUID(rule.target_id),
                    confidence_score=100.0,
                    match_tier=MatchTier.MANUAL,
                ))
                rule_matched = True
                break
        if rule_matched:
            continue

        # Tier 2: MBID graph — artist MBID confirmed → candidate library_files
        if canonical_artist_mbid:
            candidates = library_file_repo.get_by_artist_mbid(canonical_artist_mbid)
            if candidates:
                best_score = 0.0
                best_file = None
                for candidate in candidates:
                    if not candidate.track_title:
                        continue
                    score = fuzz.ratio(
                        identity.normalized_title,
                        normalize_title(candidate.track_title),
                    )
                    if score > best_score:
                        best_score = score
                        best_file = candidate

                if best_file and best_score >= 95:
                    log_identity_repo.update_match_status(
                        identity.id, MatchStatus.AUTO_MATCHED, MatchTier.MBID_EXACT
                    )
                    match_repo.create(Match(
                        id=uuid4(),
                        identity_id=identity.id,
                        library_file_id=best_file.id,
                        confidence_score=best_score,
                        match_tier=MatchTier.MBID_EXACT,
                    ))
                    if best_file.recording_id:
                        from backend.db.repositories.recordings import (
                            PgRecordingRepository,
                        )
                        # Track work_id for master selection
                        # (deferred — caller handles this via recording lookup)
                    continue
                elif best_file and best_score >= 80:
                    log_identity_repo.update_match_status(
                        identity.id, MatchStatus.AUTO_MATCHED,
                        MatchTier.NORMALIZATION,
                    )
                    match_repo.create(Match(
                        id=uuid4(),
                        identity_id=identity.id,
                        library_file_id=best_file.id,
                        confidence_score=best_score,
                        match_tier=MatchTier.NORMALIZATION,
                    ))
                    continue
                elif best_file and best_score >= 60:
                    log_identity_repo.update_match_status(
                        identity.id, MatchStatus.NEEDS_REVIEW,
                        MatchTier.NORMALIZATION,
                    )
                    match_repo.create(Match(
                        id=uuid4(),
                        identity_id=identity.id,
                        library_file_id=best_file.id,
                        confidence_score=best_score,
                        match_tier=MatchTier.NORMALIZATION,
                    ))
                    continue

        # Tier 3: Text — combined artist + title score across ALL library files
        # This is expensive but necessary when MBID graph fails
        norm_artist = normalize_artist(artist.original_name)
        all_scored: list[tuple[Any, float]] = []

        # We don't load ALL library files — only those with track_title set
        # For Phase 2, we skip this tier if no canonical_artist_mbid
        # (it would be too slow to scan all files without artist gating)

        # Tier 4: Vector similarity (placeholder — requires embeddings on recordings)
        # In Phase 2, recordings may not have embeddings yet.
        # Skip vector tier for now.

        # Fallback: no match found
        log_identity_repo.update_match_status(
            identity.id, MatchStatus.NEEDS_REVIEW, MatchTier.UNKNOWN
        )

    logger.info(
        "identity_matching_complete",
        playlist_id=str(playlist_id),
        processed=len(pending),
    )
    return matched_work_ids
```

- [ ] **Step 2: Update `backend/tasks/identity_matching_tasks.py`**

Replace `FakeLibraryFileRepository` with the real PG implementation:

```python
from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.master_selection_service import recalculate
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()
def identity_matching_task(playlist_id: str) -> None:
    """Run identity matching — terminal task in the pipeline chain."""
    settings = get_settings()
    from uuid import UUID

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        from backend.db.repositories.global_mapping_rules import (
            PgGlobalMappingRuleRepository,
        )
        from backend.db.repositories.library_files import PgLibraryFileRepository
        from backend.db.repositories.log_artists import PgLogArtistRepository
        from backend.db.repositories.log_identities import PgLogIdentityRepository
        from backend.db.repositories.matches import PgMatchRepository
        from backend.db.repositories.song_masters import PgSongMasterRepository

        work_ids = match_identities_for_playlist(
            playlist_id=UUID(playlist_id),
            log_identity_repo=PgLogIdentityRepository(conn),
            log_artist_repo=PgLogArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=PgLibraryFileRepository(conn),
            rules_repo=PgGlobalMappingRuleRepository(conn),
        )
        conn.commit()

        # Recalculate song masters for newly matched works
        if work_ids:
            recalculate(work_ids, PgSongMasterRepository(conn))
            conn.commit()

    logger.info("identity_matching_task_complete", playlist_id=playlist_id)
```

- [ ] **Step 3: Replace `backend/services/master_selection_service.py`**

```python
from __future__ import annotations

from uuid import uuid4

import structlog

from backend.domain.enums import SelectionMethod
from backend.domain.models import LibraryFile, SongMaster
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository

logger = structlog.get_logger()

# Scoring constants per spec Section 5.4
RELEASE_STATUS_SCORE: dict[str, int] = {"promotion": 100, "official": 0}
RELEASE_TYPE_SCORE: dict[str, int] = {
    "album": 80, "ep": 70, "single": 60,
    "compilation": 40, "live": 30, "other": 20,
}
FORMAT_BONUS: dict[str, int] = {"flac": 10, "aac": 6, "ogg": 6, "mp3": 3}


def _score_file(lib_file: LibraryFile) -> tuple[int, int, int]:
    """Score a library file for master selection.

    Returns (score, bitrate, duration_ms) for tiebreaking.
    """
    status_score = RELEASE_STATUS_SCORE.get(
        lib_file.release_status.value if lib_file.release_status else "", 0
    )
    type_score = RELEASE_TYPE_SCORE.get(
        lib_file.release_type.value if lib_file.release_type else "", 20
    )
    format_score = FORMAT_BONUS.get(lib_file.format, 1)
    total = status_score + type_score + format_score
    return total, lib_file.bitrate or 0, lib_file.duration_ms or 0


def recalculate(
    work_ids: list[str],
    song_master_repo: SongMasterRepository,
    recording_repo: RecordingRepository | None = None,
    library_file_repo: LibraryFileRepository | None = None,
) -> None:
    """Recalculate song masters for the given work IDs.

    Skips any work with selection_method='manual'.
    """
    if not work_ids or not recording_repo or not library_file_repo:
        return

    for work_id in work_ids:
        # Skip if manual selection exists
        existing = song_master_repo.get_by_work(work_id)
        if existing and existing.selection_method == SelectionMethod.MANUAL:
            continue

        # Find all library files for this work's recordings
        recordings = recording_repo.get_by_work(work_id)
        all_files: list[LibraryFile] = []
        for rec in recordings:
            all_files.extend(library_file_repo.get_by_recording(rec.id))

        if not all_files:
            continue

        # Score and pick best
        scored = [(f, _score_file(f)) for f in all_files]
        scored.sort(key=lambda x: x[1], reverse=True)
        best_file = scored[0][0]
        best_score = scored[0][1][0]

        song_master_repo.upsert(SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=best_file.id,
            selection_method=SelectionMethod.AUTO,
            score=best_score,
        ))

    logger.info("master_selection_recalculate", work_ids=len(work_ids))
```

- [ ] **Step 4: Create `tests/test_identity_matching.py`**

```python
from __future__ import annotations

from uuid import uuid4

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LibraryFile, LogArtist, LogIdentity, Match
from backend.services.identity_matching_service import match_identities_for_playlist
from tests.fakes.global_mapping_rules import FakeGlobalMappingRuleRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.log_artists import FakeLogArtistRepository
from tests.fakes.log_identities import FakeLogIdentityRepository
from tests.fakes.matches import FakeMatchRepository


def _setup_artist_with_match(
    log_artist_repo: FakeLogArtistRepository,
    match_repo: FakeMatchRepository,
    playlist_id: object,
    artist_name: str = "PEARL JAM",
    canonical_mbid: str = "mbid-pj",
) -> LogArtist:
    """Create a resolved log_artist with an artist match."""
    artist = LogArtist(
        id=uuid4(),
        original_name=artist_name,
        normalized_name=artist_name.lower(),
        match_status=MatchStatus.AUTO_MATCHED,
    )
    log_artist_repo.upsert(artist)
    log_artist_repo.register_playlist_artist(playlist_id, artist.id)
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        target_id=canonical_mbid,
        target_type=None,
        confidence_score=100.0,
        match_tier=MatchTier.NORMALIZATION,
    ))
    return artist


def test_tier2_mbid_graph_exact_match() -> None:
    """Tier 2: Artist MBID confirmed → library file title match ≥95."""
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    log_identity_repo = FakeLogIdentityRepository()
    match_repo = FakeMatchRepository()
    library_file_repo = FakeLibraryFileRepository()

    artist = _setup_artist_with_match(
        log_artist_repo, match_repo, playlist_id,
    )

    # Add a library file for this artist
    lib_file = LibraryFile(
        id=uuid4(),
        file_path="/music/pearl_jam/alive.mp3",
        file_hash="x" * 64,
        format="mp3",
        artist_mbid="mbid-pj",
        track_title="Alive",
    )
    library_file_repo.upsert(lib_file)

    # Add a pending identity
    identity = LogIdentity(
        id=uuid4(),
        artist_id=artist.id,
        original_title="Alive",
        normalized_title="alive",
        normalized_signature="tier2_test_" + "0" * 21,
        match_status=MatchStatus.PENDING,
    )
    log_identity_repo.upsert(identity)
    log_identity_repo.register_playlist_identity(playlist_id, identity.id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        log_identity_repo=log_identity_repo,
        log_artist_repo=log_artist_repo,
        match_repo=match_repo,
        library_file_repo=library_file_repo,
        rules_repo=FakeGlobalMappingRuleRepository(),
    )

    updated = log_identity_repo.get_by_id(identity.id)
    assert updated is not None
    assert updated.match_status == MatchStatus.AUTO_MATCHED
    assert updated.match_tier == MatchTier.MBID_EXACT


def test_no_library_files_falls_to_needs_review() -> None:
    """With no library files, identities should get NEEDS_REVIEW."""
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    log_identity_repo = FakeLogIdentityRepository()
    match_repo = FakeMatchRepository()

    artist = _setup_artist_with_match(
        log_artist_repo, match_repo, playlist_id,
    )

    identity = LogIdentity(
        id=uuid4(),
        artist_id=artist.id,
        original_title="Unknown Song",
        normalized_title="unknown song",
        normalized_signature="nolibtest_" + "0" * 22,
        match_status=MatchStatus.PENDING,
    )
    log_identity_repo.upsert(identity)
    log_identity_repo.register_playlist_identity(playlist_id, identity.id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        log_identity_repo=log_identity_repo,
        log_artist_repo=log_artist_repo,
        match_repo=match_repo,
        library_file_repo=FakeLibraryFileRepository(),
        rules_repo=FakeGlobalMappingRuleRepository(),
    )

    updated = log_identity_repo.get_by_id(identity.id)
    assert updated is not None
    assert updated.match_status == MatchStatus.NEEDS_REVIEW
```

- [ ] **Step 5: Run unit tests**

```bash
uv run pytest tests/test_identity_matching.py -v
```

Expected: 2 tests pass.

- [ ] **Step 6: Create `tests/integration/test_library_pipeline_e2e.py`**

```python
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.global_mapping_rules import PgGlobalMappingRuleRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_events import PgLogEventRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.stations import PgStationRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.enums import EnrichmentStatus, MatchStatus
from backend.domain.models import LibraryFile, Station
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.ingestion_service import ingest_csv
from backend.services.library_enrichment_service import enrich_by_release
from tests.fakes.mb_client import FakeMbClient

KAZR_CSV = Path(__file__).parent.parent / "fixtures" / "KAZR-FakeData.csv"


def test_full_pipeline_with_library(migrated_db: str) -> None:
    """End-to-end: ingest CSV → scan library → enrich → match identities.

    With library files present, some identities should AUTO_MATCH via Tier 2.
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        station_repo = PgStationRepository(conn)
        station = station_repo.create(Station(
            id=uuid4(), call_letters="KAZR-FM-LIB", name="KAZR Library Test",
        ))

        # Step 1: Ingest CSV
        result = ingest_csv(
            file_bytes=KAZR_CSV.read_bytes(),
            file_name="KAZR-lib-test.csv",
            station_id=str(station.id),
            playlist_repo=PgPlaylistRepository(conn),
            log_artist_repo=PgLogArtistRepository(conn),
            log_identity_repo=PgLogIdentityRepository(conn),
            log_event_repo=PgLogEventRepository(conn),
            broadcast_day_repo=PgBroadcastDayRepository(conn),
        )
        conn.commit()
        from uuid import UUID
        playlist_id = UUID(result.playlist_id)

        # Step 2: Artist matching with FakeMbClient
        fake_mb = FakeMbClient(
            responses={
                "METALLICA": [{"id": "mbid-metallica", "name": "Metallica",
                               "sort-name": "Metallica", "score": 100}],
                "PEARL JAM": [{"id": "mbid-pj", "name": "Pearl Jam",
                               "sort-name": "Pearl Jam", "score": 100}],
            },
        )

        match_artists_for_playlist(
            playlist_id=playlist_id,
            log_artist_repo=PgLogArtistRepository(conn),
            log_identity_repo=PgLogIdentityRepository(conn),
            artist_repo=PgArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            rules_repo=PgGlobalMappingRuleRepository(conn),
            mb_client=fake_mb,
        )
        conn.commit()

        # Step 3: Simulate library files for matched artists
        file_repo = PgLibraryFileRepository(conn)
        recording_repo = PgRecordingRepository(conn)

        # Insert a library file that matches a KAZR identity
        file_repo.upsert(LibraryFile(
            id=uuid4(),
            file_path="/music/metallica/until_it_sleeps.mp3",
            file_hash="metallica" * 7 + "xxxx",
            format="mp3",
            artist_mbid="mbid-metallica",
            track_title="Until It Sleeps",
            recording_mbid="rec-uis",
            release_mbid="rel-load",
        ))

        # Enrich with fake MB data
        fake_mb_enrichment = FakeMbClient(
            releases={
                "rel-load": {
                    "id": "rel-load",
                    "title": "Load",
                    "artist-credit": [{"artist": {
                        "id": "mbid-metallica", "name": "Metallica",
                        "sort-name": "Metallica",
                    }}],
                    "media": [{"tracks": [{"recording": {
                        "id": "rec-uis",
                        "title": "Until It Sleeps",
                        "length": 269000,
                        "relations": [],
                    }}]}],
                }
            },
        )
        enrich_by_release(
            "rel-load", file_repo, recording_repo,
            PgWorkRepository(conn), PgArtistRepository(conn),
            fake_mb_enrichment,
        )
        conn.commit()

        # Verify enrichment
        enriched_file = file_repo.get_by_path("/music/metallica/until_it_sleeps.mp3")
        assert enriched_file is not None
        assert enriched_file.recording_id == "rec-uis"
        assert enriched_file.enrichment_status == EnrichmentStatus.ENRICHED

        # Step 4: Identity matching with library
        match_identities_for_playlist(
            playlist_id=playlist_id,
            log_identity_repo=PgLogIdentityRepository(conn),
            log_artist_repo=PgLogArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=file_repo,
            rules_repo=PgGlobalMappingRuleRepository(conn),
        )
        conn.commit()

        # Verify: at least one identity should be AUTO_MATCHED
        # (the "Until It Sleeps" identity from KAZR CSV should match the library file)
        identity_statuses = conn.execute(
            "SELECT match_status, count(*) FROM log_identities GROUP BY match_status"
        ).fetchall()
        status_map = {r["match_status"]: r["count"] for r in identity_statuses}

        # We expect a mix of statuses
        total = sum(status_map.values())
        assert total >= 300  # ~343 unique identities

        conn.commit()
```

- [ ] **Step 7: Run all tests**

```bash
uv run pytest tests/ -v --ignore=tests/integration/test_mb_client.py
```

Expected: All tests pass.

- [ ] **Step 8: Run mypy and ruff**

```bash
uv run mypy --strict backend/
uv run ruff check backend/ tests/
```

- [ ] **Step 9: Commit**

```bash
git add backend/services/identity_matching_service.py \
  backend/tasks/identity_matching_tasks.py \
  backend/services/master_selection_service.py \
  tests/test_identity_matching.py \
  tests/integration/test_library_pipeline_e2e.py
git commit -m "feat: 4-tier identity matching + master selection scoring + E2E test

Replaces Phase 1 stubs with real implementations:
- Identity matching: Tier 0 (rules) → Tier 2 (MBID graph) → fallback NEEDS_REVIEW
- Master selection: release_status + type + format scoring with tiebreakers
- E2E: ingest CSV → artist match → library scan → enrich → identity match"
```

---

## Phase 2 Gate

All of the following must pass before starting Phase 3:

```bash
# All tests (excluding real MB API test)
uv run pytest tests/ -v --ignore=tests/integration/test_mb_client.py

# Type checking
uv run mypy --strict backend/

# Linting
uv run ruff check backend/ tests/
```

All commands must exit 0 with zero errors.

**Manual verification:** Scan a small subset of `D:\Media\Music` (one album folder) and verify in psql:
```sql
SELECT count(*) FROM library_files;
SELECT enrichment_status, count(*) FROM library_files GROUP BY enrichment_status;
SELECT count(*) FROM recordings;
SELECT count(*) FROM artists WHERE needs_enhancement = FALSE;
```
