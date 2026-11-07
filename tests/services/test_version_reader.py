"""Phase 2b S13 (C63, D4): the file-side version reader stops misreading.

``_VERSION_RULES`` (``classify_version_descriptor``, ``extract_version_info``) read "Album
Version", "LP Version" and "Studio Version" as remixes through the ``\\bversion\\b`` catch-all,
"Original Mix" as a remix through ``\\bmix\\b``, "Amended" as a remix, and "(I'm Special)" (The
Pretenders' "Brass in Pocket (I'm Special)") as an edition. C63 fixes those misreads on the file
side. D4 keeps the fix to them: the log's abbreviations (Lv, Unplgd, Acous...) belong to the
log-side reader (S10, PR 2), and a reading nothing misread keeps its type.

What a reading feeds, and so what these tests follow it into: grouping (a file's base title and
its recording's version), enrichment (a MusicBrainz recording's version), the untagged-file
search (its query title and its version check) and song matching's title scoring (a dash suffix
is stripped only when it reads as a version). Retyping a descriptor changes no base title, so no
title score; only "(I'm Special)" moves from recognised to not (D4).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.domain.enums import EnrichmentStatus, VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.grouping_service import assign_work
from backend.services.library_enrichment_service import recording_version_type
from backend.services.normalization import (
    classify_version_descriptor,
    extract_dash_version,
    extract_version_info,
)
from backend.services.untagged_recording_service import PickOutcome, pick_recording, query_title
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_SPECIAL = "Brass in Pocket (I'm Special)"
_SPECIAL_CURLY = "Brass in Pocket (I\u2019m Special)"

# --- the misreads (C63) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "descriptor",
    [
        "Album Version",
        "album version",
        "ALBUM VERSION",
        "LP Version",
        "lp version",
        "Studio Version",
    ],
)
def test_album_lp_and_studio_versions_read_as_originals(descriptor: str) -> None:
    assert classify_version_descriptor(descriptor) == VersionType.ORIGINAL


@pytest.mark.parametrize("descriptor", ["Original Mix", "original mix", "ORIGINAL MIX"])
def test_original_mix_reads_as_original(descriptor: str) -> None:
    assert classify_version_descriptor(descriptor) == VersionType.ORIGINAL


@pytest.mark.parametrize("descriptor", ["Amended", "amended", "Amended Version"])
def test_amended_reads_as_clean(descriptor: str) -> None:
    """ "Amended" is a clean edit wherever a catch-all used to claim it, "Amended Version" too."""
    assert classify_version_descriptor(descriptor) == VersionType.CLEAN


@pytest.mark.parametrize("descriptor", ["I'm Special", "i'm special", "I\u2019m Special"])
def test_im_special_is_not_a_version(descriptor: str) -> None:
    assert classify_version_descriptor(descriptor) == VersionType.UNKNOWN


@pytest.mark.parametrize("title", [_SPECIAL, _SPECIAL_CURLY])
def test_im_special_stays_in_the_title(title: str) -> None:
    """A title, not a version: the base title keeps it and the file is the original."""
    assert extract_version_info(title) == (title, VersionType.ORIGINAL)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Down In A Hole (Album Version)", ("Down In A Hole", VersionType.ORIGINAL)),
        ("Get Born Again (album version)", ("Get Born Again", VersionType.ORIGINAL)),
        ("No Rain [LP Version]", ("No Rain", VersionType.ORIGINAL)),
        ("Missing [LP Version]", ("Missing", VersionType.ORIGINAL)),
        ("Man In The Box (Studio Version)", ("Man In The Box", VersionType.ORIGINAL)),
        ("Insomnia (Original Mix)", ("Insomnia", VersionType.ORIGINAL)),
        ("Insomnia - Original Mix", ("Insomnia", VersionType.ORIGINAL)),
        ("Hey Ya! (Amended)", ("Hey Ya!", VersionType.CLEAN)),
        ("Hey Ya! [Amended Version]", ("Hey Ya!", VersionType.CLEAN)),
        ("Hey Ya! - Album Version", ("Hey Ya!", VersionType.ORIGINAL)),
    ],
)
def test_a_retyped_suffix_still_leaves_the_title(
    title: str, expected: tuple[str, VersionType]
) -> None:
    """Retyping keeps the descriptor recognised, so the base title is what it was."""
    assert extract_version_info(title) == expected


@pytest.mark.parametrize(
    ("title", "suffix"),
    [
        ("Hey Ya! - Album Version", "Album Version"),
        ("Hey Ya! - LP Version", "LP Version"),
        ("Hey Ya! - Studio Version", "Studio Version"),
        ("Insomnia - Original Mix", "Original Mix"),
        ("Hey Ya! - Amended", "Amended"),
    ],
)
def test_title_scoring_still_strips_a_retyped_dash_suffix(title: str, suffix: str) -> None:
    """Song matching's title scoring strips a dash suffix only when it reads as a version
    (``extract_dash_version``); the retyped ones still do, so no title score moves."""
    assert extract_dash_version(title) == (title.split(" - ")[0], suffix)


def test_title_scoring_no_longer_strips_a_dash_im_special() -> None:
    title = "Brass in Pocket - I'm Special"

    assert extract_dash_version(title) == (title, None)


# --- the fix stays narrow (D4) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        # A specific rule ahead of the catch-alls still claims the descriptor.
        ("Live Album Version", VersionType.LIVE),
        ("Explicit Album Version", VersionType.EXPLICIT),
        ("Album Version (Explicit)", VersionType.EXPLICIT),
        ("Clean Album Version", VersionType.CLEAN),
        ("Remastered Album Version", VersionType.REMASTER),
        ("Album Version Edit", VersionType.RADIO_EDIT),
        ("Acoustic LP Version", VersionType.ACOUSTIC),
        ("Live Original Mix", VersionType.LIVE),
        ("Acoustic Original Mix", VersionType.ACOUSTIC),
        ("Original Club Mix", VersionType.REMIX),
        ("Original Extended Mix", VersionType.REMIX),
        ("Original Radio Mix", VersionType.REMIX),
        ("Original Remix", VersionType.REMIX),
        ("Amended Radio Edit", VersionType.RADIO_EDIT),
        ("Special Edition", VersionType.EDITION),
        # Nor ahead of the single-word rules between them and the catch-all.
        ("Mono Album Version", VersionType.FORMAT),
        ("Deluxe Album Version", VersionType.EDITION),
        ("Bonus Studio Version", VersionType.EDITION),
        ("Alternate LP Version", VersionType.ALTERNATE),
        ("Cover Album Version", VersionType.COVER),
        ("Live Amended", VersionType.LIVE),
        ("Amended Remix", VersionType.REMIX),
        ("Amended Mono", VersionType.FORMAT),
        ("Original Rough Mix", VersionType.DEMO),
    ],
)
def test_a_specific_reading_still_wins(descriptor: str, expected: VersionType) -> None:
    """Each fix sits just ahead of the rule that misread it, so no rule above it is outranked."""
    assert classify_version_descriptor(descriptor) == expected


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        # Not on C63's list, so not retyped by this fix.
        ("Studio", VersionType.REMIX),
        ("European Version", VersionType.REMIX),
        ("UK Version", VersionType.REMIX),
        ("Album Mix", VersionType.REMIX),
        ("Mix", VersionType.REMIX),
        ("Single Version", VersionType.RADIO_EDIT),
        ("Special", VersionType.EDITION),
        ("Album", VersionType.UNKNOWN),
        ("LP", VersionType.UNKNOWN),
    ],
)
def test_a_descriptor_off_the_list_keeps_its_reading(
    descriptor: str, expected: VersionType
) -> None:
    assert classify_version_descriptor(descriptor) == expected


@pytest.mark.parametrize(
    "descriptor",
    [
        "Lv",
        "Lv.",
        "Lv. Ac.",
        "Unplgd",
        "Acous",
        "Stripped",
        "S&M2",
        "BBC",
        "Telethon",
        "Ed Sullivan",
    ],
)
def test_the_log_abbreviations_stay_off_the_file_side(descriptor: str) -> None:
    """D4: the log reader (S10) learns these. Here they stay title words, so a file's base
    title and a dash-suffixed title's score are what they were."""
    assert classify_version_descriptor(descriptor) == VersionType.UNKNOWN
    assert extract_dash_version(f"Enter Sandman - {descriptor}") == (
        f"Enter Sandman - {descriptor}",
        None,
    )


# --- grouping: which recording a new file joins --------------------------------------------------


def _grouping() -> dict[str, Any]:
    work_repo = FakeWorkRepository()
    artist_repo = FakeArtistRepository()
    work_repo.set_artist_repo(artist_repo)
    return dict(
        artist_repo=artist_repo,
        work_repo=work_repo,
        library_file_repo=FakeLibraryFileRepository(),
        recording_repo=FakeRecordingRepository(),
        song_master_repo=FakeSongMasterRepository(),
    )


def _group(repos: dict[str, Any], artist: str, title: str) -> tuple[str, str, VersionType]:
    """A new file through grouping: its work, its recording and the recording's version."""
    lf = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.mp3",
        format="mp3",
        audio=AudioMetadata(artist_name=artist, track_title=title),
    )
    result = assign_work(lf, **repos)
    assert result is not None and result.recording_id is not None
    recording = repos["recording_repo"].get_by_id(result.recording_id)
    assert recording is not None
    return result.work_id, result.recording_id, recording.version_type


@pytest.mark.parametrize(
    ("artist", "plain", "labelled"),
    [
        ("Alice In Chains", "Down In A Hole", "Down In A Hole (Album Version)"),
        ("Toad The Wet Sprocket", "Walk On The Ocean", "Walk On The Ocean [LP Version]"),
        ("Alice In Chains", "Man In The Box", "Man In The Box (Studio Version)"),
        ("Faithless", "Insomnia", "Insomnia (Original Mix)"),
        ("The Pretenders", "Brass In Pocket", _SPECIAL),
    ],
)
def test_a_new_original_joins_the_plain_files_recording(
    artist: str, plain: str, labelled: str
) -> None:
    """C63: what was read as a remix (or an edition) is the original, so a new file labelled
    that way lands on the plain file's work and recording, not on a remix recording of its own.
    "brass in pocket im special" still scores over the threshold against "brass in pocket"."""
    repos = _grouping()
    plain_work, plain_recording, _ = _group(repos, artist, plain)

    work, recording, version = _group(repos, artist, labelled)

    assert (work, recording, version) == (plain_work, plain_recording, VersionType.ORIGINAL)


def test_a_new_amended_file_gets_a_clean_recording() -> None:
    repos = _grouping()
    plain_work, plain_recording, _ = _group(repos, "OutKast", "Hey Ya!")

    work, recording, version = _group(repos, "OutKast", "Hey Ya! (Amended)")

    assert work == plain_work
    assert recording != plain_recording
    assert version == VersionType.CLEAN


def test_a_first_im_special_file_names_its_work_with_the_subtitle() -> None:
    """The subtitle is part of the title, so a work grouping creates for it keeps it."""
    repos = _grouping()

    work, _, version = _group(repos, "The Pretenders", _SPECIAL)

    created = repos["work_repo"].get_by_id(work)
    assert created is not None
    assert (created.title, version) == (_SPECIAL, VersionType.ORIGINAL)


# --- enrichment: a MusicBrainz recording's version -----------------------------------------------


def _mb_recording(title: str, disambiguation: str = "") -> dict[str, Any]:
    return {"id": "rec", "title": title, "disambiguation": disambiguation, "relations": []}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Down In A Hole (Album Version)", VersionType.ORIGINAL),
        ("No Rain (LP Version)", VersionType.ORIGINAL),
        ("Insomnia (Original Mix)", VersionType.ORIGINAL),
        ("Hey Ya! (Amended)", VersionType.CLEAN),
        (_SPECIAL, VersionType.ORIGINAL),
    ],
)
def test_enrichment_stores_the_new_reading(title: str, expected: VersionType) -> None:
    assert recording_version_type(_mb_recording(title)) == expected


def test_a_recording_musicbrainz_marks_live_stays_live() -> None:
    recording = _mb_recording("Down In A Hole (Album Version)", disambiguation="live")

    assert recording_version_type(recording) == VersionType.LIVE


# --- the untagged-file search --------------------------------------------------------------------

_ARTIST = "f9776598-2689-41db-98d3-829f5510021c"


def _untagged(title: str) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(track_title=title, duration_ms=211_000),
    )


def _hit(mbid: str, title: str) -> dict[str, Any]:
    return {
        "id": mbid,
        "title": title,
        "length": 211_000,
        "artist-credit": [{"artist": {"id": _ARTIST, "name": "Jesus Jones"}}],
    }


def test_the_search_still_drops_a_retyped_suffix() -> None:
    assert query_title("Right Here, Right Now [Album Version]") == "Right Here, Right Now"
    assert query_title("Insomnia (Original Mix)") == "Insomnia"


def test_the_search_keeps_im_special() -> None:
    assert query_title(_SPECIAL) == _SPECIAL


def test_a_plain_file_now_matches_a_hit_titled_album_version() -> None:
    """MusicBrainz's "(album version)" recording is the original, which a plain file may be."""
    hits = [_hit("r1", "Right Here, Right Now (album version)")]

    pick = pick_recording(_untagged("Right Here, Right Now"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.CONFIDENT
    assert pick.recording_mbid == "r1"


def test_an_album_version_file_no_longer_matches_a_remix_hit() -> None:
    """Both read as remixes before, so the album-version file could pick the strip mix."""
    hits = [_hit("r1", "Freaky Girl (Strip mix)")]

    pick = pick_recording(_untagged("Freaky Girl (Album Version)"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.EMPTY
