"""AudioHash: an audio fingerprint that cannot hold a malformed value."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash, InvalidAudioHashError, LibraryError, LibraryFile

MD5 = "0123456789abcdef0123456789abcdef"
SHA = "0123456789abcdef" * 4


@pytest.mark.parametrize(
    ("kind", "digest"),
    [(AudioHashKind.FLAC_MD5, MD5), (AudioHashKind.AUDIO_SHA256, SHA)],
)
def test_round_trips_through_its_stored_text(kind: AudioHashKind, digest: str) -> None:
    value = AudioHash(kind, digest)

    assert str(value) == f"{kind.value}:{digest}"
    assert AudioHash.parse(str(value)) == value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "flac-md5",
        f"flac-md5{MD5}",
        f"flac-md5:{MD5[:-1]}",
        f"flac-md5:{SHA}",
        f"audio-sha256:{MD5}",
        f"md5:{MD5}",
        f"flac-md5:{MD5.upper()}",
        f"flac-md5:{'g' * 32}",
        f"FLAC-MD5:{MD5}",
    ],
)
def test_parse_rejects_malformed_text(text: str) -> None:
    with pytest.raises(InvalidAudioHashError):
        AudioHash.parse(text)


def test_constructor_rejects_a_digest_of_the_other_kind() -> None:
    with pytest.raises(InvalidAudioHashError):
        AudioHash(AudioHashKind.AUDIO_SHA256, MD5)


def test_invalid_audio_hash_is_a_library_error() -> None:
    with pytest.raises(LibraryError):
        AudioHash.parse("nope")


def test_kind_values_are_lowercase() -> None:
    assert [k.value for k in AudioHashKind] == ["flac-md5", "audio-sha256"]


def test_a_library_file_has_no_audio_hash_until_one_is_computed() -> None:
    lf = LibraryFile(id=uuid4(), file_path="/m/a.flac", format="flac")

    assert lf.audio_hash is None
