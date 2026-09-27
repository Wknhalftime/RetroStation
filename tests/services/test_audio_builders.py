"""The test-file builders make files real tools accept."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from tests.fixtures.audio_builders import (
    add_flac_cover,
    flac_frames,
    pcm_md5,
    repad_flac,
    tag_flac,
    tag_mp3,
    write_flac,
    write_mp3,
)

LEVELS = [(100, -100), (200, -200)]


def test_flac_carries_the_md5_of_its_pcm(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS)

    info = FLAC(path).info
    assert f"{info.md5_signature:032x}" == pcm_md5(LEVELS)
    assert info.sample_rate == 44_100
    assert info.channels == 2


def test_flac_can_carry_no_md5(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS, store_md5=False)

    assert FLAC(path).info.md5_signature == 0


def test_mutagen_edits_leave_the_flac_frames_byte_identical(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS)
    tag_flac(path, {"title": "Kiss", "artist": "Prince"})
    add_flac_cover(path)
    repad_flac(path, 8_192)

    assert path.read_bytes().endswith(flac_frames(LEVELS))
    assert FLAC(path)["title"] == ["Kiss"]


def test_tag_flac_removes_tags(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS)
    tag_flac(path, {"title": "Kiss", "musicbrainz_trackid": "x"})
    tag_flac(path, {}, remove=["musicbrainz_trackid"])

    assert "musicbrainz_trackid" not in FLAC(path)


def test_mp3_has_id3v2_and_id3v1_tags(tmp_path: Path) -> None:
    path = write_mp3(tmp_path / "a.mp3")
    tag_mp3(path, "Kiss")

    data = path.read_bytes()
    assert data.startswith(b"ID3")
    assert data[-128:-125] == b"TAG"
    assert str(ID3(path)["TIT2"]) == "Kiss"
    assert MP3(path).info.length > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_decoder_decodes_the_flac_to_its_stored_md5(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS)

    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert out == f"MD5={pcm_md5(LEVELS)}"
