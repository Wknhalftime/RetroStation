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
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import mutagen
import mutagen.id3
import structlog
from mutagen._file import FileType as MutagenFileType
from mutagen._util import MutagenError

from backend.domain.enums import EnrichmentStatus, FileStatus, ReleaseStatus, ReleaseType
from backend.domain.library import AudioMetadata, LibraryFile, LibraryQuarantine
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.library_quarantine import LibraryQuarantineRepository
from backend.services.normalization import normalize_artist, normalize_title

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


def _compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_first_tag_value(tags: object, key: str) -> str | None:
    """Return tags[key][0] as a string, or None on any error."""
    if tags is None:
        return None
    try:
        val = tags[key]  # type: ignore[index]
        raw = val[0] if isinstance(val, list) else val
        # mutagen ID3 frame objects stringify to their text content
        return str(raw).strip() or None
    except (KeyError, IndexError, TypeError):
        return None


def _txxx(tags: object, desc: str) -> str | None:
    """Return value of TXXX frame with the given description, or None."""
    return _extract_first_tag_value(tags, f"TXXX:{desc}")


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


def _extract_audio_stream_metrics(audio: MutagenFileType) -> tuple[int | None, int | None]:
    """Return (duration_ms, bitrate_kbps) from audio.info, tolerating None."""
    info = audio.info  # stubs type this as StreamInfo | None
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
        return str(val).replace("\x00", "")
    except Exception:  # noqa: BLE001 — __str__ may raise anything on exotic types
        return repr(val).replace("\x00", "")


def _raw_metadata(audio: MutagenFileType) -> dict[str, str]:
    """Dump all tag frames to a plain-Python dict.

    Both keys and values are sanitised to remove null bytes (``\\x00``)
    which PostgreSQL cannot store in text/jsonb columns.
    """
    result: dict[str, str] = {}
    if audio.tags is None:
        return result
    for key, val in audio.tags.items():
        safe_key = str(key).replace("\x00", "")
        result[safe_key] = _sanitise_tag_value(val)
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

    artist_name = _extract_first_tag_value(tags, "TPE1")
    track_title = _extract_first_tag_value(tags, "TIT2")
    release_title = _extract_first_tag_value(tags, "TALB")
    track_number = _parse_slash_int(_extract_first_tag_value(tags, "TRCK"))
    disc_number = _parse_slash_int(_extract_first_tag_value(tags, "TPOS"))

    duration_ms, bitrate = _extract_audio_stream_metrics(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_compute_file_hash(path),
        format="mp3",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(
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
            artist_name=artist_name,
            normalized_artist_name=(
                normalize_artist(artist_name) if artist_name else None
            ),
            normalized_title=(
                normalize_title(track_title) if track_title else None
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Vorbis extractor (FLAC, OGG)
# ---------------------------------------------------------------------------


def _extract_vorbis(audio: MutagenFileType, path: Path, fmt: str) -> LibraryFile:
    tags = audio.tags

    recording_mbid = _extract_first_tag_value(tags, _VORBIS_RECORDING_MBID)
    artist_mbid = _extract_first_tag_value(tags, _VORBIS_ARTIST_MBID)
    album_artist_mbid = _extract_first_tag_value(tags, _VORBIS_ALBUM_ARTIST_MBID)
    release_mbid = _extract_first_tag_value(tags, _VORBIS_RELEASE_MBID)
    release_type = _to_release_type(_extract_first_tag_value(tags, "releasetype"))
    release_status = _to_release_status(_extract_first_tag_value(tags, "releasestatus"))

    artist_name = _extract_first_tag_value(tags, "artist")
    track_title = _extract_first_tag_value(tags, "title")
    release_title = _extract_first_tag_value(tags, "album")
    track_number = _parse_slash_int(_extract_first_tag_value(tags, "tracknumber"))
    disc_number = _parse_slash_int(_extract_first_tag_value(tags, "discnumber"))

    duration_ms, bitrate = _extract_audio_stream_metrics(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_compute_file_hash(path),
        format=fmt,
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(
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
            artist_name=artist_name,
            normalized_artist_name=(
                normalize_artist(artist_name) if artist_name else None
            ),
            normalized_title=(
                normalize_title(track_title) if track_title else None
            ),
        ),
    )


# ---------------------------------------------------------------------------
# WAV extractor
# ---------------------------------------------------------------------------


def _extract_wav(audio: MutagenFileType, path: Path) -> LibraryFile:
    duration_ms, _ = _extract_audio_stream_metrics(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_compute_file_hash(path),
        format="wav",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(
            duration_ms=duration_ms,
            raw_metadata=_raw_metadata(audio),
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Dispatch table: file extension → extractor function with signature
# (audio, path, fmt) -> LibraryFile.  Extensions not listed here fall through
# to the tag-type fallback and then the generic extractor.
def _dispatch_id3(audio: MutagenFileType, path: Path, _fmt: str) -> LibraryFile:
    return _extract_id3(audio, path)


def _dispatch_wav(audio: MutagenFileType, path: Path, _fmt: str) -> LibraryFile:
    return _extract_wav(audio, path)


_FORMAT_EXTRACTORS: dict[str, Callable[[MutagenFileType, Path, str], LibraryFile]] = {
    ".mp3": _dispatch_id3,
    ".flac": _extract_vorbis,
    ".ogg": _extract_vorbis,
    ".wav": _dispatch_wav,
}


@dataclass(frozen=True)
class _DiskStat:
    """The two stat() fields an incremental scan compares before hashing."""

    size: int
    mtime_ns: int


def _disk_stat(path: Path) -> _DiskStat:
    st = path.stat()
    return _DiskStat(size=st.st_size, mtime_ns=st.st_mtime_ns)


def _with_disk_stat(lf: LibraryFile, path: Path) -> LibraryFile:
    """Stamp the on-disk stat onto a freshly extracted file.

    Taken after the read, so if the file changes between now and the next
    scan the mtime moves and the file is re-read rather than trusted.
    """
    stat = _disk_stat(path)
    lf.file_size = stat.size
    lf.file_mtime_ns = stat.mtime_ns
    return lf


def _extract_by_format(audio: MutagenFileType, path: Path) -> LibraryFile:
    ext = path.suffix.lower()
    fmt = _EXT_TO_FORMAT.get(ext, ext.lstrip("."))

    # Primary dispatch: extension → extractor
    extractor = _FORMAT_EXTRACTORS.get(ext)
    if extractor is not None:
        return extractor(audio, path, fmt)

    # Tag-type fallback for files with unexpected extensions
    tag_type = type(audio.tags).__name__ if audio.tags is not None else ""
    if "ID3" in tag_type:
        return _extract_id3(audio, path)
    if "VComment" in tag_type or "Vorbis" in tag_type:
        return _extract_vorbis(audio, path, fmt)

    # Generic fallback — no tags extracted beyond format/hash/duration
    duration_ms, _ = _extract_audio_stream_metrics(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=_compute_file_hash(path),
        format=fmt,
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(
            duration_ms=duration_ms,
            raw_metadata=_raw_metadata(audio),
        ),
    )


def extract_tags(path: Path) -> LibraryFile:
    """
    Extract audio tags from *path* and return a :class:`LibraryFile`.

    Raises :exc:`mutagen.MutagenError` if the file cannot be read or parsed.
    """
    audio: MutagenFileType | None = mutagen.File(str(path), easy=False)  # type: ignore[attr-defined]
    if audio is None:
        raise MutagenError(f"mutagen could not identify file: {path}")
    return _with_disk_stat(_extract_by_format(audio, path), path)


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

    if total == 0:
        logger.warning("scan_directory_no_candidates", root=str(root))

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


@dataclass
class FolderScanResult:
    """Result counts from an incremental per-folder scan."""

    files_written: int = 0
    files_skipped: int = 0
    files_missing: int = 0
    files_reappeared: int = 0
    quarantined: int = 0
    # Legacy rows (no stored stat) confirmed unchanged and stamped with
    # their stat so the next scan can skip them on stat() alone.
    files_stat_backfilled: int = 0
    # New paths whose content matched a row whose file had moved away; the
    # row was repointed, keeping its links, instead of a bare row inserted.
    files_relocated: int = 0
    # The folder exists but could not be listed (permissions, a share
    # dropping mid-scan). Nothing was diffed and nothing marked missing.
    folder_unreadable: bool = False
    # Quarantine entries dropped because their file read cleanly or is gone.
    quarantine_cleared: int = 0
    # Every path that failed this visit; their quarantine entries stand.
    failing_paths: set[str] = field(default_factory=set)

    def record_failure(self, file_path: str) -> None:
        self.quarantined += 1
        self.failing_paths.add(file_path)


def quarantine_once(
    quarantine_repo: LibraryQuarantineRepository, file_path_str: str, error: str,
) -> None:
    """Quarantine a file unless it already is; every scan that reaches it retries it."""
    if quarantine_repo.get_by_path(file_path_str) is not None:
        return
    quarantine_repo.create_write_only(
        LibraryQuarantine(id=uuid4(), file_path=file_path_str, error_message=error)
    )


def clear_resolved_quarantine(
    paths: Iterable[str],
    still_failing: set[str],
    quarantine_repo: LibraryQuarantineRepository,
) -> int:
    """Drop the quarantine entries in *paths* whose file did not fail this visit.

    Call only after a visit that reached every file those paths could name:
    a file that did not fail either read cleanly or is no longer on disk,
    and either way its entry is stale. Returns the number of paths cleared.
    """
    resolved = [p for p in paths if p not in still_failing]
    for path in resolved:
        quarantine_repo.delete_by_path(path)
    return len(resolved)


def _extract_tags_safe(
    path: Path,
    file_path_str: str,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> LibraryFile | None:
    """Extract tags from path; on any failure quarantine the file and return None."""
    try:
        return extract_tags(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scan_smart_quarantine", path=file_path_str, error=str(exc))
        quarantine_once(quarantine_repo, file_path_str, str(exc))
        result.record_failure(file_path_str)
        return None


def _mark_missing_files(
    existing_by_path: dict[str, LibraryFile],
    file_repo: LibraryFileRepository,
    result: FolderScanResult,
) -> None:
    """Mark files that are in DB but absent from disk as MISSING."""
    for file_path_str, existing in existing_by_path.items():
        if existing.file_status == FileStatus.PRESENT:
            file_repo.mark_missing(file_path_str)
            result.files_missing += 1


def _reextract_and_upsert(
    path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> None:
    lf = _extract_tags_safe(path, str(path), quarantine_repo, result)
    if lf is not None:
        file_repo.upsert(lf)
        result.files_written += 1


def _is_gone_or_same_file(old: Path, new: Path) -> bool:
    """True when *old* no longer exists, or names *new* under another spelling.

    The second case is a case-only rename on a case-insensitive filesystem,
    where the old spelling still resolves to the renamed file.
    """
    if not os.path.exists(old):
        return True
    try:
        return os.path.samefile(old, new)
    except OSError:
        return False


def _moved_from(lf: LibraryFile, file_repo: LibraryFileRepository) -> LibraryFile | None:
    """The row this newly seen file was moved or renamed from, if any.

    A row with identical content whose own file is still on disk is a
    duplicate copy, not the origin of a move, and is left alone.
    """
    if lf.file_hash is None:
        return None
    for candidate in file_repo.get_by_hash(lf.file_hash):
        if candidate.file_path != lf.file_path and _is_gone_or_same_file(
            Path(candidate.file_path), Path(lf.file_path),
        ):
            return candidate
    return None


def adopt_moved_row(lf: LibraryFile, file_repo: LibraryFileRepository) -> str | None:
    """Repoint the row *lf* was moved or renamed from at *lf*'s path.

    For a path the DB has no row for. Keeps the old row's id and every
    grouping/enrichment link that a bare insert would leave behind.
    Returns the old path, or None when *lf* is not a move.
    """
    origin = _moved_from(lf, file_repo)
    if origin is None:
        return None
    file_repo.relocate(origin.id, lf.file_path)
    return origin.file_path


def mark_unseen_missing(
    root: Path, seen_paths: set[str], file_repo: LibraryFileRepository,
) -> int:
    """Mark PRESENT files under *root* that a complete walk did not see as MISSING.

    A walk that saw nothing marks nothing: a root that is absent, or an
    empty mount-point folder, almost always means a drive that is not
    plugged in, not a library that was deleted. Returns the count marked.
    """
    if not seen_paths:
        logger.warning("mark_unseen_missing_skipped_empty_walk", root=str(root))
        return 0
    unseen = [
        path
        for path, status in file_repo.get_path_statuses_under(str(root)).items()
        if status == FileStatus.PRESENT and path not in seen_paths
    ]
    for path in unseen:
        file_repo.mark_missing(path)
    return len(unseen)


def _index_new_file(
    path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> str | None:
    """Scenario 3: index a path the DB has no row for.

    A moved or renamed file adopts its old row so grouping and enrichment
    links survive. Returns the old path it was moved from, else None.
    """
    lf = _extract_tags_safe(path, str(path), quarantine_repo, result)
    if lf is None:
        return None
    moved_from = adopt_moved_row(lf, file_repo)
    if moved_from is not None:
        result.files_relocated += 1
    file_repo.upsert(lf)
    result.files_written += 1
    return moved_from


def _restore_reappeared_file(
    existing: LibraryFile,
    path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> None:
    """Scenario 4: a MISSING row is back on disk. Same content → restore
    PRESENT and keep enrichment; otherwise re-extract."""
    try:
        current_hash = _compute_file_hash(path)
    except OSError as exc:
        logger.warning("hash_failed", path=str(path), error=str(exc))
        result.record_failure(str(path))
        return

    if current_hash == existing.file_hash:
        existing.file_status = FileStatus.PRESENT
        file_repo.upsert(existing)
    else:
        _reextract_and_upsert(path, file_repo, quarantine_repo, result)
    result.files_reappeared += 1


def _stat_matches(existing: LibraryFile, disk: _DiskStat) -> bool | None:
    """Compare stored size+mtime with disk. None when the row predates stat tracking."""
    if existing.file_size is None or existing.file_mtime_ns is None:
        return None
    return existing.file_size == disk.size and existing.file_mtime_ns == disk.mtime_ns


def _modified_since_indexed(existing: LibraryFile, disk: _DiskStat) -> bool:
    indexed_ns = int(existing.indexed_at.timestamp() * 1_000_000_000)
    return disk.mtime_ns > indexed_ns


def _reconcile_present_file(
    existing: LibraryFile,
    path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> None:
    """Scenarios 1 and 2 for a row already PRESENT in the DB.

    The content hash means reading every byte, so it is the last resort:

    - stored stat matches disk           → unchanged, skip unread
    - stored stat differs                → modified, re-extract
    - no stored stat (legacy row):
        - file older than its index row  → unchanged, backfill stat, skip unread
        - file newer                     → hash; equal → backfill, else re-extract
    """
    try:
        disk = _disk_stat(path)
    except OSError as exc:
        logger.warning("stat_failed", path=str(path), error=str(exc))
        result.record_failure(str(path))
        return

    matches = _stat_matches(existing, disk)
    if matches is True:
        result.files_skipped += 1
        return
    if matches is False:
        _reextract_and_upsert(path, file_repo, quarantine_repo, result)
        return

    # Legacy row. Only a file touched after we last read it can differ.
    if _modified_since_indexed(existing, disk):
        try:
            current_hash = _compute_file_hash(path)
        except OSError as exc:
            logger.warning("hash_failed", path=str(path), error=str(exc))
            result.record_failure(str(path))
            return
        if current_hash != existing.file_hash:
            _reextract_and_upsert(path, file_repo, quarantine_repo, result)
            return

    file_repo.update_file_stat(existing.id, disk.size, disk.mtime_ns)
    result.files_skipped += 1
    result.files_stat_backfilled += 1


def _list_audio_files(folder_path: Path) -> dict[str, Path] | None:
    """Audio files directly in *folder_path*; empty if it is gone, None if unlistable.

    Gone and unreadable must stay distinct: gone marks every file missing,
    while a folder we merely failed to list still holds its files.
    """
    if not folder_path.is_dir():
        return {}
    try:
        return {
            str(entry): entry
            for entry in folder_path.iterdir()
            if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS
        }
    except OSError as exc:
        logger.warning("scan_folder_unreadable", path=str(folder_path), error=str(exc))
        return None


def scan_folder_incrementally(
    *,
    folder_path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
) -> FolderScanResult:
    """Incremental scan of a single folder — diffs disk vs DB, handles all 6 scenarios.

    Only processes files directly in this folder (not recursive).

    Scenarios:
      1. Unchanged file (stat, else hash, matches) -> skip, no DB write
      2. Modified file (stat or hash differs)      -> re-extract tags, upsert (resets enrichment)
      3. New file (not in DB)                      -> extract tags, insert; if moved
                                                      or renamed, adopt the old row
      4. Re-appeared file (MISSING in DB)          -> restore PRESENT, keep enrichment if hash same
      5. Missing file (in DB, not on disk)         -> mark MISSING
      6. Parse failure (Mutagen error)             -> quarantine (once per path)

    Afterwards, quarantine entries for this folder's files that did not fail
    this visit are dropped: the file now reads, or is gone.
    """
    result = FolderScanResult()

    existing_by_path: dict[str, LibraryFile] = {
        f.file_path: f
        for f in file_repo.get_by_folder_path(str(folder_path))
    }

    disk_files = _list_audio_files(folder_path)
    if disk_files is None:
        result.folder_unreadable = True
        return result

    for file_path_str, path in disk_files.items():
        existing = existing_by_path.pop(file_path_str, None)
        if existing is None:
            moved_from = _index_new_file(path, file_repo, quarantine_repo, result)
            if moved_from is not None:
                # A rename within this folder: the old spelling is no longer
                # a row to mark missing.
                existing_by_path.pop(moved_from, None)
        elif existing.file_status == FileStatus.MISSING:
            _restore_reappeared_file(existing, path, file_repo, quarantine_repo, result)
        else:
            _reconcile_present_file(existing, path, file_repo, quarantine_repo, result)

    _mark_missing_files(existing_by_path, file_repo, result)

    # Non-recursive, like the rest of the visit: a child folder's entries
    # are judged when that folder is visited.
    in_folder = {
        p for p in quarantine_repo.get_paths_under(str(folder_path))
        if Path(p).parent == folder_path
    }
    result.quarantine_cleared = clear_resolved_quarantine(
        in_folder, result.failing_paths, quarantine_repo,
    )

    return result
