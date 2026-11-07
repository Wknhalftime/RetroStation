"""A NUL in a UFID frame (phase 2a S2): PostgreSQL text refuses it, which would lose a scan's chunk.

Some taggers terminate the frame's data with one. S2's text is otherwise as ``TXXX``'s, unvalidated.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import ID3, TIT2, UFID

from backend.services.audio_tags import read_tags
from tests.fixtures.audio_builders import write_mp3

RECORDING = "0b000000-0000-4000-8000-000000000001"


def _mp3_with_ufid(path: Path, data: bytes) -> Path:
    write_mp3(path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Song"]))
    tags.add(UFID(owner="http://musicbrainz.org", data=data))
    tags.save(str(path))
    return path


def test_a_terminating_nul_is_dropped(tmp_path: Path) -> None:
    path = _mp3_with_ufid(tmp_path / "a.mp3", RECORDING.encode("ascii") + b"\x00")

    assert read_tags(path).audio.recording_mbid == RECORDING


def test_only_a_nul_is_no_mbid(tmp_path: Path) -> None:
    path = _mp3_with_ufid(tmp_path / "a.mp3", b"\x00")

    assert read_tags(path).audio.recording_mbid is None
