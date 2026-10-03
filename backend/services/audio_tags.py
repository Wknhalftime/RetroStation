"""
Audio tag and file reading — mutagen extraction for the library scanner.

Public API:
  read_tags(path) -> LibraryFile  (tags, stat, a FLAC's stored audio MD5;
                                    raises OSError on a file it cannot stat,
                                    MutagenError on an unreadable one)
  probe_clip(path) -> ProbedClip  (a sign-off clip's format and length, from
                                    its content; raises UnsupportedClipError
                                    or UnreadableClipError, never a mutagen
                                    or OS error)

Supported formats: .flac, .mp3, .m4a, .ogg, .wav

Split from library_scan_service.py (AUD-009): this module reads a single
file's tags; it never walks a directory, talks to a repository or decides
scan/reconcile policy, and must never import library_scan_service.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import mutagen
import mutagen.flac
import mutagen.id3
import mutagen.mp3
import mutagen.wave
from mutagen._file import FileType as MutagenFileType
from mutagen._util import MutagenError

from backend.domain.enums import EnrichmentStatus, ReleaseStatus, ReleaseType
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile
from backend.domain.streaming import (
    ClipFormat,
    ProbedClip,
    UnreadableClipError,
    UnsupportedClipError,
)
from backend.services.audio_hash import compute_audio_hash
from backend.services.normalization import normalize_artist, normalize_title

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".wav"})

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
            normalized_artist_name=(normalize_artist(artist_name) if artist_name else None),
            normalized_title=(normalize_title(track_title) if track_title else None),
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
            normalized_artist_name=(normalize_artist(artist_name) if artist_name else None),
            normalized_title=(normalize_title(track_title) if track_title else None),
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
class DiskStat:
    """The two stat() fields a scan compares to decide whether to re-read a file's tags."""

    size: int
    mtime_ns: int


def disk_stat(path: Path) -> DiskStat:
    st = path.stat()
    return DiskStat(size=st.st_size, mtime_ns=st.st_mtime_ns)


def _with_disk_stat(lf: LibraryFile, stat: DiskStat) -> LibraryFile:
    """Stamp the on-disk stat onto a freshly extracted file.

    *stat* is taken before the file is parsed, so a file rewritten while
    or after it is read keeps a stored stat that no longer matches, and
    the next scan re-reads it rather than trusting it.
    """
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
    # Bind tags to a local variable so mypy understands type narrowing.
    # mutagen.FileType.tags is declared as None, so direct checks don't narrow.
    tags: object | None = audio.tags
    tag_type = type(tags).__name__ if tags is not None else ""
    if "ID3" in tag_type:
        return _extract_id3(audio, path)
    if "VComment" in tag_type or "Vorbis" in tag_type:
        return _extract_vorbis(audio, path, fmt)

    # Generic fallback — no tags extracted beyond format and duration
    duration_ms, _ = _extract_audio_stream_metrics(audio)

    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        format=fmt,
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(
            duration_ms=duration_ms,
            raw_metadata=_raw_metadata(audio),
        ),
    )


def _stored_audio_hash(audio: MutagenFileType, path: Path) -> AudioHash | None:
    """The flac-md5 a FLAC's STREAMINFO carries, confirmed by re-walking its metadata.

    None when the format is not FLAC, when the encoder stored no MD5, or (Task 4's
    guard, applied by :func:`compute_audio_hash`) when the file has no audio frames
    after its metadata — a stored MD5 alone must not stand in for audio that was
    never written. Re-walking the headers only seeks; it never reads audio bytes.
    """
    info = audio.info
    if not isinstance(info, mutagen.flac.StreamInfo) or info.md5_signature == 0:
        return None
    return compute_audio_hash(path)


def read_tags(path: Path) -> LibraryFile:
    """
    Tags, format, on-disk stat and — for a FLAC that stores one — the audio MD5,
    reading only the file's header and tag blocks.

    A file without a stored MD5 (every MP3, a FLAC with none) is left with no
    audio hash: its audio is not read here. Raises :exc:`OSError` if the file
    cannot be stat'ed, :exc:`mutagen.MutagenError` if it cannot be read or parsed.
    """
    stat = disk_stat(path)
    audio: MutagenFileType | None = mutagen.File(str(path), easy=False)  # type: ignore[attr-defined]
    if audio is None:
        raise MutagenError(f"mutagen could not identify file: {path}")
    lf = _with_disk_stat(_extract_by_format(audio, path), stat)
    lf.audio_hash = _stored_audio_hash(audio, path)
    return lf


# ---------------------------------------------------------------------------
# Sign-off clip probe (D26; PG3, I2)
# ---------------------------------------------------------------------------

# The detected mutagen type decides the clip's format; a type not listed is unsupported.
_CLIP_FORMATS: tuple[tuple[type[MutagenFileType], ClipFormat], ...] = (
    (mutagen.flac.FLAC, ClipFormat.FLAC),
    (mutagen.mp3.MP3, ClipFormat.MP3),
    (mutagen.wave.WAVE, ClipFormat.WAV),
)

_NOT_AUDIO = "the file is not audio that can be read"


def _clip_format(audio: MutagenFileType) -> ClipFormat:
    for kind, clip_format in _CLIP_FORMATS:
        if isinstance(audio, kind):
            return clip_format
    raise UnsupportedClipError(
        f"the clip is {type(audio).__name__} audio; a sign-off must be FLAC, MP3 or WAV"
    )


def _clip_span_ms(info: object) -> int:
    """The clip's length in whole milliseconds; an MP3 mutagen calls ``sketchy`` (its frame
    sync matched, but the stream is doubtful) or a missing, zero or non-finite length is not
    readable audio."""
    if getattr(info, "sketchy", False) is True:
        raise UnreadableClipError(f"{_NOT_AUDIO} (doubtful MP3 stream)")
    length = getattr(info, "length", None)
    if not isinstance(length, int | float) or not math.isfinite(length) or length <= 0:
        raise UnreadableClipError(f"{_NOT_AUDIO} (no length)")
    return round(length * 1000)


def probe_clip(path: Path) -> ProbedClip:
    """A staged sign-off clip's format and length, read from its content only (I2).

    The file's name never helps: the store stages clips as ``<uuid4>.partial``.

    Raises:
        UnsupportedClipError: the content is audio of a type other than FLAC, MP3 or WAV.
        UnreadableClipError: the content is not audio that can be read, or has no length.
    """
    try:
        audio: MutagenFileType | None = mutagen.File(str(path))  # type: ignore[attr-defined]
    except (MutagenError, OSError) as unreadable:
        raise UnreadableClipError(f"{_NOT_AUDIO} ({unreadable})") from unreadable
    if audio is None:
        raise UnreadableClipError(_NOT_AUDIO)
    clip_format = _clip_format(audio)
    return ProbedClip(clip_format, _clip_span_ms(audio.info))
