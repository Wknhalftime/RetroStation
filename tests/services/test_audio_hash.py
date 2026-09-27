"""Audio fingerprints: unchanged by tags, cover art and padding; changed by the audio."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash
from backend.services.audio_hash import compute_audio_hash, stored_flac_md5
from tests.fixtures.audio_builders import (
    add_flac_cover,
    add_mp3_cover,
    flac_frames,
    mp3_frames,
    pcm_md5,
    repad_flac,
    repad_mp3,
    tag_flac,
    tag_mp3,
    write_flac,
    write_mp3,
)

LEVELS = [(100, -100), (200, -200)]
EDITED = [(101, -100), (200, -200)]  # one sample value differs: an audio edit

Edit = Callable[[Path], None]

FLAC_EDITS: dict[str, Edit] = {
    "retag": lambda p: tag_flac(p, {"title": "Kiss (Extended)", "album": "Parade"}),
    "cover": add_flac_cover,
    "padding": lambda p: repad_flac(p, 16_384),
}
MP3_EDITS: dict[str, Edit] = {
    "retag": lambda p: tag_mp3(p, "Kiss (Extended Version)"),
    "cover": add_mp3_cover,
    "padding": lambda p: repad_mp3(p, 8_192),
}


def _flac(
    tmp_path: Path,
    name: str = "a.flac",
    *,
    store_md5: bool = True,
    levels: list[tuple[int, int]] = LEVELS,
) -> Path:
    path = write_flac(tmp_path / name, levels, store_md5=store_md5)
    tag_flac(path, {"title": "Kiss", "artist": "Prince"})
    return path


def _mp3(tmp_path: Path, name: str = "a.mp3", *, payload: int = 0) -> Path:
    path = write_mp3(tmp_path / name, payload=payload)
    tag_mp3(path, "Kiss")
    return path


@pytest.mark.parametrize("store_md5", [True, False], ids=["stored-md5", "md5-zero"])
@pytest.mark.parametrize("edit", FLAC_EDITS.values(), ids=FLAC_EDITS.keys())
def test_flac_tag_cover_and_padding_edits_keep_the_hash(
    tmp_path: Path, store_md5: bool, edit: Edit
) -> None:
    path = _flac(tmp_path, store_md5=store_md5)
    before, bytes_before = compute_audio_hash(path), path.read_bytes()

    edit(path)

    assert path.read_bytes() != bytes_before  # the edit really rewrote the file
    assert compute_audio_hash(path) == before


@pytest.mark.parametrize("store_md5", [True, False], ids=["stored-md5", "md5-zero"])
def test_flac_audio_edit_changes_the_hash(tmp_path: Path, store_md5: bool) -> None:
    original = _flac(tmp_path, "a.flac", store_md5=store_md5)
    edited = _flac(tmp_path, "b.flac", store_md5=store_md5, levels=EDITED)

    assert compute_audio_hash(original) != compute_audio_hash(edited)


def test_flac_with_a_stored_md5_is_fingerprinted_by_it(tmp_path: Path) -> None:
    path = _flac(tmp_path)

    assert compute_audio_hash(path) == AudioHash(AudioHashKind.FLAC_MD5, pcm_md5(LEVELS))


def test_the_stored_md5_is_what_mutagen_reads(tmp_path: Path) -> None:
    path = _flac(tmp_path)

    assert compute_audio_hash(path) == stored_flac_md5(FLAC(path).info.md5_signature)


def test_flac_without_a_stored_md5_hashes_its_frames(tmp_path: Path) -> None:
    path = _flac(tmp_path, store_md5=False)

    expected = hashlib.sha256(flac_frames(LEVELS)).hexdigest()
    assert compute_audio_hash(path) == AudioHash(AudioHashKind.AUDIO_SHA256, expected)


@pytest.mark.parametrize("edit", MP3_EDITS.values(), ids=MP3_EDITS.keys())
def test_mp3_tag_cover_and_padding_edits_keep_the_hash(tmp_path: Path, edit: Edit) -> None:
    path = _mp3(tmp_path)
    before, bytes_before = compute_audio_hash(path), path.read_bytes()

    edit(path)

    assert path.read_bytes() != bytes_before
    assert compute_audio_hash(path) == before


def test_mp3_hash_skips_its_id3v2_and_id3v1_tags(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    data = path.read_bytes()
    assert data.startswith(b"ID3") and data[-128:-125] == b"TAG"

    expected = hashlib.sha256(mp3_frames()).hexdigest()
    assert compute_audio_hash(path) == AudioHash(AudioHashKind.AUDIO_SHA256, expected)


def test_mp3_audio_edit_changes_the_hash(tmp_path: Path) -> None:
    assert compute_audio_hash(_mp3(tmp_path, "a.mp3")) != compute_audio_hash(
        _mp3(tmp_path, "b.mp3", payload=1)
    )


def test_untagged_mp3_hashes_every_byte(tmp_path: Path) -> None:
    path = write_mp3(tmp_path / "a.mp3")

    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert compute_audio_hash(path) == AudioHash(AudioHashKind.AUDIO_SHA256, expected)


@pytest.mark.parametrize("name", ["a.ogg", "a.m4a", "a.wav"])
def test_other_formats_have_no_fingerprint(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"\x00" * 64)

    assert compute_audio_hash(path) is None


def test_an_mp3_that_is_only_tags_has_no_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "empty.mp3"
    path.write_bytes(b"")
    tag_mp3(path, "Nothing")

    assert compute_audio_hash(path) is None


def test_a_flac_extension_on_something_else_has_no_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "fake.flac"
    path.write_bytes(b"\x00" * 100)

    assert compute_audio_hash(path) is None


def test_a_truncated_flac_has_no_fingerprint(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "cut.flac", LEVELS, store_md5=False)
    path.write_bytes(path.read_bytes()[:20])

    assert compute_audio_hash(path) is None


def test_a_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        compute_audio_hash(tmp_path / "gone.flac")


def test_no_stored_md5_means_no_flac_md5() -> None:
    assert stored_flac_md5(0) is None
