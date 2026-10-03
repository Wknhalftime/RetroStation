"""The real clip probe: ``probe_clip`` reads the format and length from the content (PR G1,
Task 3; traceability Z: T3.24-T3.26, T3.28).

Requirements: review ruling I2 (detect the real format from the content; a type other than
FLAC, MP3 or WAV is unsupported, so an OGG renamed ``.mp3`` is refused; random bytes are not
audio; mutagen's MP3 frame sync is permissive, so an MP3 it calls ``sketchy`` is
unreadable); PG3 (FLAC, MP3 or WAV). The file is named ``*.partial``, as the store stages
it, so the name can never help the detection. WAVs are made with the stdlib ``wave`` module
and the other inputs from a fixed seed: no encoder is needed.
"""

from __future__ import annotations

import io
import random
import wave
from pathlib import Path
from types import SimpleNamespace

import mutagen
import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from backend.domain.streaming import (
    ClipFormat,
    ProbedClip,
    UnreadableClipError,
    UnsupportedClipError,
)
from backend.services.audio_tags import probe_clip

RATE = 8_000


def wav_bytes(seconds: float) -> bytes:
    """A silent mono 16-bit WAV of exactly ``seconds``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"\x00\x00" * round(seconds * RATE))
    return buffer.getvalue()


def staged(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "3f2b9c1e-0000-4000-8000-000000000000.partial"
    path.write_bytes(data)
    return path


def test_a_wav_is_detected_with_its_length(tmp_path: Path) -> None:
    # T3.24 (I2): the format from the content, the length in whole milliseconds.
    assert probe_clip(staged(tmp_path, wav_bytes(2.5))) == ProbedClip(ClipFormat.WAV, 2_500)


@pytest.mark.parametrize(
    "data",
    [random.Random(1995).randbytes(64 * 1024), b"", wav_bytes(2.5)[:20]],
    ids=["random-64KiB", "empty", "truncated-wav-header"],
)
def test_bytes_that_are_not_audio_are_unreadable(tmp_path: Path, data: bytes) -> None:
    # T3.25 (I2): never a format guessed from noise, never a raw mutagen or OS error.
    with pytest.raises(UnreadableClipError):
        probe_clip(staged(tmp_path, data))


def test_an_mp3_that_mutagen_calls_sketchy_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T3.26 (I2): mutagen found an MP3 frame sync, but says the stream is doubtful. The same
    # result without the doubt is an MP3 of its length (the contrast).
    detected = MP3.__new__(MP3)
    monkeypatch.setattr(mutagen, "File", lambda *args, **kwargs: detected)
    path = staged(tmp_path, b"\xff\xfb\x90\x00" + bytes(4096))

    detected.info = SimpleNamespace(length=12.0, sketchy=True)  # type: ignore[assignment]
    with pytest.raises(UnreadableClipError):
        probe_clip(path)

    detected.info = SimpleNamespace(length=12.0, sketchy=False)  # type: ignore[assignment]
    assert probe_clip(path) == ProbedClip(ClipFormat.MP3, 12_000)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (FLAC, ClipFormat.FLAC),
        (WAVE, ClipFormat.WAV),
        (MP3, ClipFormat.MP3),
        (OggVorbis, None),
        (MP4, None),
    ],
    ids=["flac", "wave", "mp3", "ogg-vorbis", "mp4"],
)
def test_the_detected_type_decides_the_format_and_others_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: type, expected: ClipFormat | None
) -> None:
    # T3.28 (I2, PG3; audit MF1): the type mutagen detects maps to FLAC, MP3 or WAV; any other
    # type is unsupported (415 at the route), never guessed and never "unreadable".
    info = SimpleNamespace(length=3.0, sketchy=False)
    detected_kind = type(f"Detected{kind.__name__}", (kind,), {"info": info})
    detected = detected_kind.__new__(detected_kind)
    monkeypatch.setattr(mutagen, "File", lambda *args, **kwargs: detected)
    path = staged(tmp_path, b"\x00" * 64)
    if expected is None:
        with pytest.raises(UnsupportedClipError):
            probe_clip(path)
    else:
        assert probe_clip(path) == ProbedClip(expected, 3_000)
