"""The tag reader keeps every artist MBID and name (phase 2a S1; C26, C69, M2).

Real FLAC and MP3 files, as a tagger writes them. ``artist_mbids`` holds every value of the
artist-MBID tag and ``credit`` pairs them with the ``artists`` tag. The scan does no lookup, so a
name the tags can't give stays null. The name columns and the scalar ``artist_mbid`` stay as they
were, because 465 files would otherwise leave same-name song matching (C26).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from mutagen.oggvorbis import OggVorbis

from backend.domain.library import CreditEntry
from backend.services.artist_credit import credit_from_raw_metadata
from backend.services.audio_tags import read_tags
from backend.services.normalization import normalize_artist
from tests.fixtures.audio_builders import write_flac, write_mp3

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "audio"


def _flac(path: Path, **tags: list[str]) -> Path:
    write_flac(path, [(0, 0)])
    audio = FLAC(str(path))
    for key, values in tags.items():
        audio[key] = values
    audio.save()
    return path


def _mp3(
    path: Path, artist: list[str] | None, mbids: list[str], names: list[str] | None = None
) -> Path:
    write_mp3(path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Song"]))
    if artist is not None:
        tags.add(TPE1(encoding=3, text=artist))
    if mbids:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=mbids))
    if names is not None:
        tags.add(TXXX(encoding=3, desc="ARTISTS", text=names))
    tags.save(str(path))
    return path


def test_a_flac_keeps_every_mbid_and_name(tmp_path: Path) -> None:
    path = _flac(
        tmp_path / "a.flac",
        artist=["A & B"],
        artists=["A", "B"],
        musicbrainz_artistid=[ONE, TWO],
    )

    audio = read_tags(path).audio

    assert audio.artist_mbids == (ONE, TWO)
    assert audio.credit == (CreditEntry(ONE, "A", " & "), CreditEntry(TWO, "B", ""))


def test_an_mp3_keeps_every_mbid_and_name(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", ["A feat. B"], [ONE, TWO], ["A", "B"])

    audio = read_tags(path).audio

    assert audio.artist_mbids == (ONE, TWO)
    assert audio.credit == (CreditEntry(ONE, "A", " feat. "), CreditEntry(TWO, "B", ""))


@pytest.mark.parametrize("separator", [", ", "; ", "/"])
def test_a_flac_with_mbids_joined_in_one_value(tmp_path: Path, separator: str) -> None:
    path = _flac(
        tmp_path / "a.flac",
        artist=["A & B"],
        artists=["A", "B"],
        musicbrainz_artistid=[f"{ONE}{separator}{TWO}"],
    )

    audio = read_tags(path).audio

    assert audio.artist_mbids == (ONE, TWO)
    assert audio.credit == (CreditEntry(ONE, "A", " & "), CreditEntry(TWO, "B", ""))


def test_an_mp3_with_mbids_joined_in_one_value(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", ["A & B"], [f"{ONE}/{TWO}"], ["A", "B"])

    assert read_tags(path).audio.artist_mbids == (ONE, TWO)


def test_names_the_tags_cant_give_stay_null(tmp_path: Path) -> None:
    """No ``artists`` tag: the scan does no lookup, and enrichment names them (C69)."""
    path = _flac(tmp_path / "a.flac", artist=["A & B"], musicbrainz_artistid=[ONE, TWO])

    assert read_tags(path).audio.credit == (CreditEntry(ONE), CreditEntry(TWO))


def test_counts_that_differ_stay_null(tmp_path: Path) -> None:
    path = _flac(
        tmp_path / "a.flac", artist=["A & B"], artists=["A"], musicbrainz_artistid=[ONE, TWO]
    )

    assert read_tags(path).audio.credit == (CreditEntry(ONE), CreditEntry(TWO))


def test_one_mbid_pairs_with_the_whole_text(tmp_path: Path) -> None:
    path = _flac(tmp_path / "a.flac", artist=["Hall & Oates"], musicbrainz_artistid=[ONE])

    audio = read_tags(path).audio

    assert audio.artist_mbids == (ONE,)
    assert audio.credit == (CreditEntry(ONE, "Hall & Oates", ""),)


def test_an_untagged_flac_has_no_mbids_and_no_credit(tmp_path: Path) -> None:
    audio = read_tags(_flac(tmp_path / "a.flac", artist=["A"])).audio

    assert audio.artist_mbids == ()
    assert audio.credit is None


def test_an_untagged_mp3_has_no_mbids_and_no_credit(tmp_path: Path) -> None:
    audio = read_tags(_mp3(tmp_path / "a.mp3", ["A"], [])).audio

    assert audio.artist_mbids == ()
    assert audio.credit is None


def test_a_file_with_no_tags_has_no_mbids_and_no_credit() -> None:
    audio = read_tags(AUDIO / "no_tags.wav").audio

    assert audio.artist_mbids == ()
    assert audio.credit is None


def test_an_ogg_reads_as_a_flac_does(tmp_path: Path) -> None:
    path = tmp_path / "a.ogg"
    shutil.copyfile(AUDIO / "minimal_tags.ogg", path)
    tags = OggVorbis(str(path))
    tags["artist"] = ["A & B"]
    tags["artists"] = ["A", "B"]
    tags["musicbrainz_artistid"] = [ONE, TWO]
    tags.save()

    audio = read_tags(path).audio

    assert audio.artist_mbids == (ONE, TWO)
    assert audio.credit == (CreditEntry(ONE, "A", " & "), CreditEntry(TWO, "B", ""))


def test_an_untagged_ogg_has_no_mbids_and_no_credit() -> None:
    audio = read_tags(AUDIO / "minimal_tags.ogg").audio

    assert audio.artist_mbids == ()
    assert audio.credit is None


def test_a_flac_keeps_its_name_columns_and_its_scalar(tmp_path: Path) -> None:
    """C26: the first value, as ever. Nothing moves out of same-name matching."""
    path = _flac(
        tmp_path / "a.flac",
        artist=["A & B", "Another Print"],
        artists=["A", "B"],
        musicbrainz_artistid=[ONE, TWO],
    )

    audio = read_tags(path).audio

    assert audio.artist_name == "A & B"
    assert audio.normalized_artist_name == normalize_artist("A & B")
    assert audio.artist_mbid == ONE


def test_an_mp3_keeps_its_name_columns_and_its_scalar(tmp_path: Path) -> None:
    path = _mp3(tmp_path / "a.mp3", ["A & B", "Another Print"], [ONE, TWO], ["A", "B"])

    audio = read_tags(path).audio

    assert audio.artist_name == "A & B"
    assert audio.normalized_artist_name == normalize_artist("A & B")
    assert audio.artist_mbid == ONE


def test_a_joined_scalar_stays_joined(tmp_path: Path) -> None:
    """The scalar is untouched even when the columns split it: S6's reading counts on that."""
    joined = f"{ONE}, {TWO}"
    path = _flac(tmp_path / "a.flac", artist=["A & B"], musicbrainz_artistid=[joined])

    assert read_tags(path).audio.artist_mbid == joined


def test_raw_metadata_is_as_it_was(tmp_path: Path) -> None:
    path = _flac(tmp_path / "a.flac", artist=["A"], artists=["A"], musicbrainz_artistid=[ONE])

    raw = read_tags(path).audio.raw_metadata

    assert raw is not None
    assert raw["musicbrainz_artistid"] == f"['{ONE}']"
    assert raw["artists"] == "['A']"


def test_slashes_in_an_artists_value_are_one_name(tmp_path: Path) -> None:
    """An ID3v2.3 tagger's joined names. M32: never split, so the counts differ and the names
    wait for MusicBrainz."""
    path = _mp3(tmp_path / "a.mp3", ["AC/DC & B"], [ONE, TWO], ["AC/DC/B"])

    assert read_tags(path).audio.credit == (CreditEntry(ONE), CreditEntry(TWO))


def test_a_nul_in_a_name_is_dropped(tmp_path: Path) -> None:
    """JSONB refuses one, and a file that scanned before must not start to quarantine."""
    path = _flac(
        tmp_path / "a.flac",
        artist=["AB & C"],
        artists=["A\x00B", "C"],
        musicbrainz_artistid=[ONE, TWO],
    )

    assert read_tags(path).audio.credit == (
        CreditEntry(ONE, "AB", " & "),
        CreditEntry(TWO, "C", ""),
    )


@pytest.mark.parametrize(
    "tags",
    [
        {"artist": [" A & B "], "artists": [" A ", "B"], "musicbrainz_artistid": [ONE, TWO]},
        {"artist": [""], "artists": ["A", "B"], "musicbrainz_artistid": [ONE, TWO]},
        {"artist": ["A & B"], "artists": ["", "B"], "musicbrainz_artistid": [ONE, TWO]},
        {"artists": ["Earth, Wind & Fire", "it's"], "musicbrainz_artistid": [ONE, TWO]},
        {"artist": ['Say "Hi"'], "musicbrainz_artistid": [ONE]},
        {"artist": ["   "], "musicbrainz_artistid": [ONE]},
        {"artist": ["A"], "musicbrainz_artistid": ["bad", ONE]},
        {"artist": ["A"], "musicbrainz_artistid": [f"{ONE}, {TWO}"]},
        {"artist": ["A"]},
    ],
    ids=[
        "padded",
        "blank print",
        "blank name",
        "a comma and a quote",
        "double quotes",
        "spaces",
        "a malformed value",
        "joined",
        "untagged",
    ],
)
def test_the_scan_and_the_backfill_agree(tmp_path: Path, tags: dict[str, list[str]]) -> None:
    """The same file through the reader and through what it saved. They are two code paths to one
    pair of columns, and a difference would change a file's credit at its next re-tag."""
    audio = read_tags(_flac(tmp_path / "a.flac", **tags)).audio
    assert audio.raw_metadata is not None

    assert credit_from_raw_metadata(audio.raw_metadata) == (audio.artist_mbids, audio.credit)
