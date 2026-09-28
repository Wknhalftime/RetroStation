"""Build small, real FLAC and MP3 files in code, for audio-fingerprint tests.

FLAC: a STREAMINFO block, then one frame per 4096-sample block whose two
subframes are CONSTANT (a single 16-bit value per channel), with valid CRCs,
so decoders accept it. STREAMINFO carries the MD5 of the decoded PCM (16-bit
little-endian, interleaved) exactly as an encoder writes it, or zero.
MP3: MPEG-1 Layer III frame headers with a filler payload; mutagen parses
it, it is not meant to be played. Tags, cover art and padding are written by
mutagen, as a tagger would write them.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TIT2, Encoding  # type: ignore[attr-defined]

Levels = Sequence[tuple[int, int]]

SAMPLE_RATE = 44_100
BLOCK_SIZE = 4096
_PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
_MP3_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])  # MPEG-1 L3, 128 kbps, 44.1 kHz
_MP3_FRAME_SIZE = 417


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _frame(number: int, left: int, right: int) -> bytes:
    # Sync + fixed block size; block size code 12 (4096); rate code 9 (44.1 kHz);
    # two independent channels; 16-bit samples. Frame number UTF-8 coded.
    header = bytes([0xFF, 0xF8, (12 << 4) | 9, (1 << 4) | (4 << 1)])
    header += chr(number).encode("utf-8")
    header += bytes([_crc8(header)])
    body = b"".join(b"\x00" + struct.pack(">h", value) for value in (left, right))
    frame = header + body
    return frame + struct.pack(">H", _crc16(frame))


def flac_frames(levels: Levels) -> bytes:
    """The audio frames write_flac writes for *levels*: the bytes after the metadata."""
    return b"".join(_frame(i, left, right) for i, (left, right) in enumerate(levels))


def pcm_md5(levels: Levels) -> str:
    """MD5 of the decoded PCM, as a FLAC encoder stores it in STREAMINFO (32 hex)."""
    digest = hashlib.md5()  # noqa: S324 - FLAC's own checksum, not security
    for left, right in levels:
        digest.update(struct.pack("<hh", left, right) * BLOCK_SIZE)
    return digest.hexdigest()


def _streaminfo(total_samples: int, md5: bytes) -> bytes:
    packed = (SAMPLE_RATE << 44) | (1 << 41) | (15 << 36) | total_samples
    body = struct.pack(">HH", BLOCK_SIZE, BLOCK_SIZE) + b"\x00" * 6
    body += packed.to_bytes(8, "big") + md5
    return bytes([0x80]) + len(body).to_bytes(3, "big") + body  # the last metadata block


def write_flac(path: Path, levels: Levels, *, store_md5: bool = True) -> Path:
    """An untagged FLAC of *levels*; STREAMINFO's MD5 is zero unless *store_md5*."""
    md5 = bytes.fromhex(pcm_md5(levels)) if store_md5 else bytes(16)
    info = _streaminfo(BLOCK_SIZE * len(levels), md5)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + info + flac_frames(levels))
    return path


def tag_flac(path: Path, tags: Mapping[str, str], *, remove: Iterable[str] = ()) -> None:
    """Set and remove Vorbis comments, as a tagger does; the frames are untouched."""
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = [value]
    for key in remove:
        del audio[key]
    audio.save()


def _cover() -> Picture:
    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = "image/png"
    picture.data = _PNG_STUB
    return picture


def add_flac_cover(path: Path) -> None:
    audio = FLAC(path)
    audio.add_picture(_cover())
    audio.save()


def repad_flac(path: Path, padding: int) -> None:
    FLAC(path).save(padding=lambda _info: padding)


def mp3_frames(*, payload: int = 0, frames: int = 8) -> bytes:
    """MPEG frames whose payload bytes all equal *payload*; changing it is an audio edit."""
    frame = _MP3_HEADER + bytes([payload]) * (_MP3_FRAME_SIZE - len(_MP3_HEADER))
    return frame * frames


def write_mp3(path: Path, *, payload: int = 0, frames: int = 8) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mp3_frames(payload=payload, frames=frames))
    return path


def _id3(path: Path) -> ID3:
    return ID3(path) if path.read_bytes()[:3] == b"ID3" else ID3()


def tag_mp3(path: Path, title: str) -> None:
    """Write an ID3v2.3 title, plus an ID3v1 tag at the end of the file."""
    tags = _id3(path)
    tags.add(TIT2(encoding=Encoding.UTF8, text=[title]))
    tags.save(str(path), v1=2, v2_version=3)


def add_mp3_cover(path: Path) -> None:
    tags = _id3(path)
    tags.add(APIC(encoding=Encoding.UTF8, mime="image/png", type=3, desc="", data=_PNG_STUB))
    tags.save(str(path), v1=2, v2_version=3)


def repad_mp3(path: Path, padding: int) -> None:
    _id3(path).save(str(path), v1=2, v2_version=3, padding=lambda _info: padding)
