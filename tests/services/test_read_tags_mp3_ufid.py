"""The ID3 reader takes the recording MBID from UFID when TXXX has none (phase 2a S2; C26).

Picard writes the recording MBID to a UFID frame owned by ``http://musicbrainz.org``, and the
reader only ever looked at ``TXXX:MusicBrainz Track Id``. It reaches a file's ``recording_mbid`` at
its next full scan, not through the backfill.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TXXX, UFID

from backend.services.audio_tags import read_tags
from tests.fixtures.audio_builders import write_flac, write_mp3

RECORDING = "0b000000-0000-4000-8000-000000000001"
OTHER = "0b000000-0000-4000-8000-000000000002"
MUSICBRAINZ = "http://musicbrainz.org"


def _mp3(path: Path, *frames: object) -> Path:
    write_mp3(path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Song"]))
    for frame in frames:
        tags.add(frame)
    tags.save(str(path))
    return path


def test_ufid_gives_the_recording_mbid(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", UFID(owner=MUSICBRAINZ, data=RECORDING.encode("ascii")))

    assert read_tags(path).audio.recording_mbid == RECORDING


def test_txxx_wins_when_both_are_there(tmp_path: Path) -> None:
    path = _mp3(
        tmp_path / "a.mp3",
        TXXX(encoding=3, desc="MusicBrainz Track Id", text=[RECORDING]),
        UFID(owner=MUSICBRAINZ, data=OTHER.encode("ascii")),
    )

    assert read_tags(path).audio.recording_mbid == RECORDING


def test_another_owners_ufid_is_no_mbid(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", UFID(owner="http://example.com", data=b"anything"))

    assert read_tags(path).audio.recording_mbid is None


def test_no_ufid_and_no_txxx_is_no_mbid(tmp_path: Path) -> None:
    assert read_tags(_mp3(tmp_path / "a.mp3")).audio.recording_mbid is None


def test_bytes_that_arent_text_are_no_mbid(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", UFID(owner=MUSICBRAINZ, data=b"\xff\xfe\x00"))

    assert read_tags(path).audio.recording_mbid is None


def test_an_empty_ufid_is_no_mbid(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", UFID(owner=MUSICBRAINZ, data=b""))

    assert read_tags(path).audio.recording_mbid is None


def test_a_flac_reads_its_tag_as_ever(tmp_path: Path) -> None:
    path = tmp_path / "a.flac"
    write_flac(path, [(0, 0)])
    audio = FLAC(str(path))
    audio["musicbrainz_trackid"] = [RECORDING]
    audio.save()

    assert read_tags(path).audio.recording_mbid == RECORDING
