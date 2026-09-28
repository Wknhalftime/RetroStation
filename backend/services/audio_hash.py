"""Audio fingerprints that ignore tags: what a file sounds like, not how it is labelled.

A FLAC encoder stores the MD5 of the decoded audio in STREAMINFO, so a FLAC
that has one is fingerprinted by reading its header. A FLAC without one (the
MD5 is all zero) and an MP3 are fingerprinted by SHA-256 over their audio
bytes only: a FLAC's frames after its metadata blocks; an MP3 without its
leading ID3v2 and trailing ID3v1 tag. Neither changes when tags, cover art or
padding do. Other formats, and files with no audio bytes, get no fingerprint.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash

_ID3V2_MARKER = b"ID3"
_ID3V2_HEADER_SIZE = 10
_ID3V2_FOOTER_FLAG = 0x10
_ID3V1_MARKER = b"TAG"
_ID3V1_SIZE = 128
_FLAC_MARKER = b"fLaC"
_FLAC_BLOCK_HEADER_SIZE = 4
_FLAC_LAST_BLOCK = 0x80
_FLAC_BLOCK_TYPE = 0x7F
_STREAMINFO = 0
_STREAMINFO_MD5 = slice(18, 34)
_READ_CHUNK = 1 << 20


class AudioHasher(Protocol):
    """Fingerprints one file's audio; None when it has none. Raises OSError."""

    def __call__(self, path: Path) -> AudioHash | None: ...


def stored_flac_md5(md5_signature: int) -> AudioHash | None:
    """The flac-md5 fingerprint for a STREAMINFO MD5; None when the encoder stored none."""
    if md5_signature == 0:
        return None
    return AudioHash(AudioHashKind.FLAC_MD5, f"{md5_signature:032x}")


def _id3v2_size(head: bytes) -> int:
    """Bytes a leading ID3v2 tag takes (header, body, footer); 0 without one."""
    if len(head) < _ID3V2_HEADER_SIZE or not head.startswith(_ID3V2_MARKER):
        return 0
    body = 0
    for byte in head[6:10]:  # syncsafe: 7 bits per byte
        body = (body << 7) | (byte & 0x7F)
    footer = _ID3V2_HEADER_SIZE if head[5] & _ID3V2_FOOTER_FLAG else 0
    return _ID3V2_HEADER_SIZE + body + footer


@dataclass(frozen=True)
class _FlacLayout:
    frames_offset: int
    stored_md5: int


def _flac_layout(fh: BinaryIO) -> _FlacLayout | None:
    """Where a FLAC's frames start, and its STREAMINFO MD5; None if it is not a FLAC."""
    fh.seek(_id3v2_size(fh.read(_ID3V2_HEADER_SIZE)))
    if fh.read(len(_FLAC_MARKER)) != _FLAC_MARKER:
        return None
    md5, last = 0, False
    while not last:
        header = fh.read(_FLAC_BLOCK_HEADER_SIZE)
        if len(header) < _FLAC_BLOCK_HEADER_SIZE:
            return None
        last = bool(header[0] & _FLAC_LAST_BLOCK)
        length = int.from_bytes(header[1:], "big")
        body_start = fh.tell()
        if header[0] & _FLAC_BLOCK_TYPE == _STREAMINFO:
            body = fh.read(length)
            if len(body) < length:
                return None
            md5 = int.from_bytes(body[_STREAMINFO_MD5], "big")
        fh.seek(body_start + length)
    return _FlacLayout(frames_offset=fh.tell(), stored_md5=md5)


def _sha256_of_range(fh: BinaryIO, start: int, end: int) -> AudioHash | None:
    """audio-sha256 over the bytes [start, end); None when the range is empty."""
    if end <= start:
        return None
    digest = hashlib.sha256()
    fh.seek(start)
    remaining = end - start
    while remaining > 0:
        chunk = fh.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    return AudioHash(AudioHashKind.AUDIO_SHA256, digest.hexdigest())


def _flac_hash(fh: BinaryIO, size: int) -> AudioHash | None:
    layout = _flac_layout(fh)
    if layout is None or layout.frames_offset >= size:
        return None
    stored = stored_flac_md5(layout.stored_md5)
    if stored is not None:
        return stored
    return _sha256_of_range(fh, layout.frames_offset, size)


def _mp3_audio_end(fh: BinaryIO, size: int) -> int:
    """Where an MP3's audio ends: before a trailing ID3v1 tag when it has one."""
    if size < _ID3V1_SIZE:
        return size
    fh.seek(size - _ID3V1_SIZE)
    has_id3v1 = fh.read(len(_ID3V1_MARKER)) == _ID3V1_MARKER
    return size - _ID3V1_SIZE if has_id3v1 else size


def _mp3_hash(fh: BinaryIO, size: int) -> AudioHash | None:
    start = _id3v2_size(fh.read(_ID3V2_HEADER_SIZE))
    return _sha256_of_range(fh, start, _mp3_audio_end(fh, size))


_HASHERS: dict[str, Callable[[BinaryIO, int], AudioHash | None]] = {
    ".flac": _flac_hash,
    ".mp3": _mp3_hash,
}


def compute_audio_hash(path: Path) -> AudioHash | None:
    """*path*'s audio fingerprint; None for another format or a file with no audio.

    Raises OSError if the file cannot be read.
    """
    hasher = _HASHERS.get(path.suffix.lower())
    if hasher is None:
        return None
    with path.open("rb") as fh:
        return hasher(fh, os.fstat(fh.fileno()).st_size)
