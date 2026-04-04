"""
Library scan service — tag extraction and directory walking.

Public API:
  extract_tags(path)       -> LibraryFile  (raises MutagenError on unreadable file)
  scan_directory(root, on_progress=None)  -> (list[LibraryFile], list[LibraryQuarantine])

Supported formats: .flac, .mp3, .m4a, .ogg, .wav
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import mutagen
import mutagen.id3
import structlog
from mutagen._file import FileType as MutagenFileType
from mutagen._util import MutagenError

from backend.domain.enums import EnrichmentStatus, ReleaseStatus, ReleaseType
from backend.domain.models import LibraryFile, LibraryQuarantine

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".m4a", ".ogg", ".wav"}
)

_EXT_TO_FORMAT: dict[str, str] = {
    ".flac": "flac",
    ".mp3": "mp3",
    ".m4a": "aac",
    ".ogg": "ogg",
    ".wav": "wav",
}

# TXXX frame description strings written by MusicBrainz Picard (ID3 / MP3).
_TXXX_RECORDING_MBID = "MusicBrainz Track Id"
_TXXX_ARTIST_MBID = "MusicBrainz Artist Id"
_TXXX_ALBUM_ARTIST_MBID = "MusicBrainz Album Artist Id"
_TXXX_RELEASE_MBID = "MusicBrainz Album Id"
_TXXX_RELEASE_TYPE = "MusicBrainz Release Type"
_TXXX_RELEASE_STATUS = "MusicBrainz Release Status"

# Vorbis comment tag names (FLAC / OGG) — lowercase.
_VORBIS_RECORDING_MBID = "musicbrainz_trackid"
_VORBIS_ARTIST_MBID = "musicbrainz_artistid"
_VORBIS_ALBUM_ARTIST_MBID = "musicbrainz_albumartistid"
_VORBIS_RELEASE_MBID = "musicbrainz_albumid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _first(tags: Any, key: str) -> str | None:
    """Return tags[key][0] as a string, or None on any error."""
    if tags is None:
        return None
    try:
        val = tags[key]
        raw = val[0] if isinstance(val, list) else val
        # mutagen ID3 frame objects stringify to their text content
        return str(raw).strip() or None
    except (KeyError, IndexError, TypeError):
        return None


def _txxx(tags: Any, desc: str) -> str | None:
    """Return value of TXXX frame with the given description, or None."""
    return _first(tags, f"TXXX:{desc}")


def _parse_slash_int(value: str | None) -> int | None:
    """Parse '6/8' → 6, '6' → 6, None → None."""
    if not value:
        return None
    try:
        return int(value.split("/")[0])
    except (ValueError, IndexError):
        return None


def _to_release_type(value: str | None) -> ReleaseType | None:
    if not value:
        return None
    try:
        return ReleaseType(value.strip().lower())
    except ValueError:
        return None


def _to_release_status(value: str | None) -> ReleaseStatus | None:
    if not value:
        return None
    try:
        return ReleaseStatus(value.strip().lower())
    except ValueError:
        return None


def _duration_and_bitrate(audio: MutagenFileType) -> tuple[int | None, int | None]:
    """Return (duration_ms, bitrate_kbps) from audio.info, tolerating None."""
    info: Any = audio.info  # stubs type this as StreamInfo | None
    if info is None:
        return None, None
    duration_ms: int | None = None
    bitrate: int | None = None
    with contextlib.suppress(AttributeError, TypeError):
        duration_ms = int(info.length * 1000)
    with contextlib.suppress(AttributeError, TypeError):
        bitrate = int(info.bitrate) // 1000
    return duration_ms, bitrate


def _sanitise_tag_value(val: object) -> str:
    """Convert a tag value to a string safe for PostgreSQL text/jsonb columns.

    Strips null bytes (``\\x00``) which PostgreSQL cannot store in text.
    """
    try:
        s = str(val)
    except Exception:
        s = repr(val)
    return s.replace("\x00", "")


def _raw_metadata(audio: MutagenFileType) -> dict[str, Any]:
    """Dump all tag frames to a plain-Python dict."""
    result: dict[str, Any] = {}
    if audio.tags is None:
        return result
    for key, val in audio.tags.items():
        result[key] = _sanitise_tag_value(val)
    return result


# ---------------------------------------------------------------------------
# ID3 extractor (MP3)
# ---------------------------------------------------------------------------


def _extract_id3(audio: MutagenFileType, path: Path) -> LibraryFile:
    tags = audio.tags  # mutagen.id3.ID3 or None

    recording_mbid = _txxx(tags, _TXXX_RECORDING_MBID)
    artist_mbid = _txxx(tags, _TXXX_ARTIST_MBID)
    album_artist_mbid = _txxx(tags, _TXXX_ALBUM_ARTIST_MBID)
    release_mbid = _txxx(tags, _TXXX_RELEASE_MBID)
    release_type = _to_release_type(_txxx(tags, _TXXX_RELEASE_TYPE))
    release_status = _to_release_status(_txxx(tags, _TXXX_RELEASE_STATUS))

    track_title = _first(tags, "TIT2")
    release_title = _first(tags, "TALB")
    track_number = _parse_slash_int(_first(tags, "TRCK"))
    disc_number = _parse_slash_int(_first(tags, "TPOS"))

    duration_ms, bitrate = _duration_and_bitrate(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_sha256(path),
        format="mp3",
        enrichment_status=EnrichmentStatus.PENDING,
        recording_mbid=recording_mbid,
        artist_mbid=artist_mbid,
        album_artist_mbid=album_artist_mbid,
        release_mbid=release_mbid,
        release_title=release_title,
        release_type=release_type,
        release_status=release_status,
        track_title=track_title,
        track_number=track_number,
        disc_number=disc_number,
        duration_ms=duration_ms,
        bitrate=bitrate,
        raw_metadata=_raw_metadata(audio),
    )


# ---------------------------------------------------------------------------
# Vorbis extractor (FLAC, OGG)
# ---------------------------------------------------------------------------


def _extract_vorbis(audio: MutagenFileType, path: Path, fmt: str) -> LibraryFile:
    tags = audio.tags

    recording_mbid = _first(tags, _VORBIS_RECORDING_MBID)
    artist_mbid = _first(tags, _VORBIS_ARTIST_MBID)
    album_artist_mbid = _first(tags, _VORBIS_ALBUM_ARTIST_MBID)
    release_mbid = _first(tags, _VORBIS_RELEASE_MBID)
    release_type = _to_release_type(_first(tags, "releasetype"))
    release_status = _to_release_status(_first(tags, "releasestatus"))

    track_title = _first(tags, "title")
    release_title = _first(tags, "album")
    track_number = _parse_slash_int(_first(tags, "tracknumber"))
    disc_number = _parse_slash_int(_first(tags, "discnumber"))

    duration_ms, bitrate = _duration_and_bitrate(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_sha256(path),
        format=fmt,
        enrichment_status=EnrichmentStatus.PENDING,
        recording_mbid=recording_mbid,
        artist_mbid=artist_mbid,
        album_artist_mbid=album_artist_mbid,
        release_mbid=release_mbid,
        release_title=release_title,
        release_type=release_type,
        release_status=release_status,
        track_title=track_title,
        track_number=track_number,
        disc_number=disc_number,
        duration_ms=duration_ms,
        bitrate=bitrate,
        raw_metadata=_raw_metadata(audio),
    )


# ---------------------------------------------------------------------------
# WAV extractor
# ---------------------------------------------------------------------------


def _extract_wav(audio: MutagenFileType, path: Path) -> LibraryFile:
    duration_ms, _ = _duration_and_bitrate(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_sha256(path),
        format="wav",
        enrichment_status=EnrichmentStatus.PENDING,
        duration_ms=duration_ms,
        raw_metadata=_raw_metadata(audio),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_tags(path: Path) -> LibraryFile:
    """
    Extract audio tags from *path* and return a :class:`LibraryFile`.

    Raises :exc:`mutagen.MutagenError` if the file cannot be read or parsed.
    """
    audio: MutagenFileType | None = mutagen.File(str(path), easy=False)  # type: ignore[attr-defined]
    if audio is None:
        raise MutagenError(f"mutagen could not identify file: {path}")

    ext = path.suffix.lower()
    fmt = _EXT_TO_FORMAT.get(ext, ext.lstrip("."))

    # Dispatch by tag type
    tag_type = type(audio.tags).__name__ if audio.tags is not None else ""

    if ext == ".mp3" or "ID3" in tag_type:
        return _extract_id3(audio, path)

    if ext in {".flac", ".ogg"} or "VComment" in tag_type or "Vorbis" in tag_type:
        return _extract_vorbis(audio, path, fmt)

    if ext == ".wav":
        return _extract_wav(audio, path)

    # Generic fallback — no tags extracted beyond format/hash/duration
    duration_ms, _ = _duration_and_bitrate(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_sha256(path),
        format=fmt,
        enrichment_status=EnrichmentStatus.PENDING,
        duration_ms=duration_ms,
        raw_metadata=_raw_metadata(audio),
    )


def scan_directory(
    root: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_file: Callable[[LibraryFile], None] | None = None,
    on_quarantine: Callable[[LibraryQuarantine], None] | None = None,
) -> tuple[list[LibraryFile], list[LibraryQuarantine]]:
    """
    Walk *root* recursively and extract tags from all supported audio files.

    Returns ``(files, quarantine)`` where *quarantine* contains an entry for
    every file that raised a :exc:`mutagen.MutagenError`.

    Optional callbacks:
      *on_file* — called with each successfully extracted :class:`LibraryFile`.
      *on_quarantine* — called with each :class:`LibraryQuarantine` entry.
      *on_progress* — called with ``(processed, total, current_path)`` every
        50 files and on the final file.
    """
    candidates = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    total = len(candidates)

    files: list[LibraryFile] = []
    quarantine: list[LibraryQuarantine] = []

    for processed_idx, path in enumerate(candidates, start=1):
        try:
            lf = extract_tags(path)
            files.append(lf)
            if on_file is not None:
                on_file(lf)
        except MutagenError as exc:
            logger.warning("Quarantining %s: %s", path, exc)
            entry = LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=str(exc),
            )
            quarantine.append(entry)
            if on_quarantine is not None:
                on_quarantine(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error scanning %s: %s", path, exc)
            entry = LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=f"{type(exc).__name__}: {exc}",
            )
            quarantine.append(entry)
            if on_quarantine is not None:
                on_quarantine(entry)

        if on_progress is not None and (
            processed_idx % 50 == 0 or processed_idx == total
        ):
            on_progress(processed_idx, total, str(path))

    return files, quarantine
