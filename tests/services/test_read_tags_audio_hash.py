"""read_tags takes a FLAC's stored MD5 with its tags, and reads nothing more."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash
from backend.services.audio_hash import compute_audio_hash
from backend.services.audio_tags import read_tags
from tests.fixtures.audio_builders import (
    flac_frames,
    pcm_md5,
    tag_flac,
    tag_mp3,
    write_flac,
    write_mp3,
)

LEVELS = [(300, -300), (400, -400)]
_NO_BYTE_HASHING = patch(
    "backend.services.audio_hash._sha256_of_range",
    side_effect=AssertionError("read_tags hashed audio bytes"),
)


def test_flac_with_a_stored_md5_gets_its_fingerprint_with_its_tags(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS)
    tag_flac(path, {"title": "Kiss", "artist": "Prince"})

    with _NO_BYTE_HASHING:
        lf = read_tags(path)

    assert lf.audio_hash == compute_audio_hash(path)
    assert lf.audio.track_title == "Kiss"


def test_flac_without_a_stored_md5_is_left_for_the_backfill(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "a.flac", LEVELS, store_md5=False)
    tag_flac(path, {"title": "Kiss"})

    with _NO_BYTE_HASHING:
        assert read_tags(path).audio_hash is None


def test_mp3_is_left_for_the_backfill(tmp_path: Path) -> None:
    path = write_mp3(tmp_path / "a.mp3")
    tag_mp3(path, "Kiss")

    with _NO_BYTE_HASHING:
        assert read_tags(path).audio_hash is None


def test_flac_with_a_stored_md5_is_fingerprinted_without_reading_audio_bytes(
    tmp_path: Path,
) -> None:
    """The fingerprint itself — not just its absence — survives the byte-hashing guard."""
    path = write_flac(tmp_path / "a.flac", LEVELS)
    tag_flac(path, {"title": "Kiss"})

    with _NO_BYTE_HASHING:
        lf = read_tags(path)

    assert lf.audio_hash == AudioHash(AudioHashKind.FLAC_MD5, pcm_md5(LEVELS))


def test_flac_truncated_to_metadata_length_has_no_fingerprint(tmp_path: Path) -> None:
    """A stored MD5 alone must not stand in for audio that was never written (Task 4's guard).

    Checked empirically: mutagen parses a FLAC cut right after its metadata (no frames)
    without error, so read_tags must return audio_hash=None rather than raising.
    """
    path = write_flac(tmp_path / "a.flac", LEVELS, store_md5=True)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - len(flac_frames(LEVELS))])

    assert read_tags(path).audio_hash is None
