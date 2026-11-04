"""The confidence rule of the untagged-file pass: which search hit, if any, is the file.

A file with no MusicBrainz tags whose work's artist has an MBID is searched by
(artist MBID, title). MusicBrainz lists many recordings of one song by one
artist under the same title (album, single, live, duplicates), so a title match
alone never picks; the rule is stated in ``pick_recording``'s docstring and
checked here case by case.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.untagged_recording_service import (
    DURATION_TOLERANCE_MS,
    PickOutcome,
    pick_recording,
    query_title,
    search_title,
)

_ARTIST = "f9776598-2689-41db-98d3-829f5510021c"
_OTHER_ARTIST = "00000000-0000-4000-8000-0000000000aa"


def _file(title: str, duration_ms: int | None = 211_000) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{title}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(track_title=title, duration_ms=duration_ms),
    )


def _hit(
    mbid: str,
    title: str = "Right Here, Right Now",
    *,
    length: int | None = 211_000,
    artist: str = _ARTIST,
    disambiguation: str | None = None,
) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "id": mbid,
        "title": title,
        "artist-credit": [{"artist": {"id": artist, "name": "Jesus Jones"}}],
    }
    if length is not None:
        hit["length"] = length
    if disambiguation is not None:
        hit["disambiguation"] = disambiguation
    return hit


def test_search_title_is_the_normalized_title_without_its_version_suffix() -> None:
    assert search_title("Right Here, Right Now [Album Version]") == "right here right now"
    assert search_title("Crash Into Me [Edit]") == "crash into me"
    assert search_title("Hourglass (live)") == "hourglass"


def test_query_title_keeps_the_title_as_written_without_its_version_suffix() -> None:
    assert query_title("He Thinks He'll Keep Her [Radio Edit]") == "He Thinks He'll Keep Her"
    assert query_title("Right Here,  Right Now") == "Right Here, Right Now"


def test_one_hit_with_the_title_and_the_length_is_confident() -> None:
    pick = pick_recording(_file("Right Here, Right Now [Album Version]"), _ARTIST, [_hit("r1")])

    assert pick.outcome is PickOutcome.CONFIDENT
    assert pick.recording_mbid == "r1"


def test_no_hit_is_empty() -> None:
    pick = pick_recording(_file("Right Here, Right Now"), _ARTIST, [])

    assert pick.outcome is PickOutcome.EMPTY
    assert pick.recording_mbid is None


def test_a_hit_with_another_title_is_not_a_candidate() -> None:
    hits = [_hit("r1", title="International Bright Young Thing")]

    pick = pick_recording(_file("Right Here, Right Now"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.EMPTY


def test_a_hit_not_credited_to_the_artist_is_not_a_candidate() -> None:
    pick = pick_recording(
        _file("Right Here, Right Now"), _ARTIST, [_hit("r1", artist=_OTHER_ARTIST)]
    )

    assert pick.outcome is PickOutcome.EMPTY


def test_two_hits_within_the_tolerance_are_ambiguous() -> None:
    hits = [_hit("r1", length=211_000), _hit("r2", length=211_000 + DURATION_TOLERANCE_MS)]

    pick = pick_recording(_file("Right Here, Right Now"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.AMBIGUOUS
    assert pick.recording_mbid is None
    assert pick.candidate_mbids == ("r1", "r2")


def test_hits_outside_the_tolerance_are_ambiguous_not_picked() -> None:
    """The title matched but no length did: a version MusicBrainz does not list."""
    hits = [_hit("r1", length=250_000), _hit("r2", length=180_000)]

    pick = pick_recording(_file("Right Here, Right Now"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.AMBIGUOUS
    assert pick.recording_mbid is None


def test_a_hit_without_a_length_cannot_be_the_one() -> None:
    hits = [_hit("r1", length=None), _hit("r2", length=211_500)]

    pick = pick_recording(_file("Right Here, Right Now"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.CONFIDENT
    assert pick.recording_mbid == "r2"


def test_a_file_without_a_duration_is_confident_only_with_a_single_candidate() -> None:
    one = pick_recording(_file("Right Here, Right Now", None), _ARTIST, [_hit("r1")])
    two = pick_recording(
        _file("Right Here, Right Now", None), _ARTIST, [_hit("r1"), _hit("r2", length=250_000)]
    )

    assert one.outcome is PickOutcome.CONFIDENT and one.recording_mbid == "r1"
    assert two.outcome is PickOutcome.AMBIGUOUS


def test_a_live_hit_never_matches_a_studio_file() -> None:
    hits = [_hit("r1", disambiguation="live, 1991-02-12: Brixton Academy")]

    pick = pick_recording(_file("Right Here, Right Now"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.EMPTY


def test_a_live_file_matches_only_a_live_hit() -> None:
    hits = [_hit("r1"), _hit("r2", title="Right Here, Right Now (live)", length=211_200)]

    pick = pick_recording(_file("Right Here, Right Now (Live)"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.CONFIDENT
    assert pick.recording_mbid == "r2"


def test_a_plain_titled_hit_matches_a_file_labelled_as_an_edit() -> None:
    """MusicBrainz titles an edit with the plain song name; the length tells them apart."""
    hits = [_hit("r1", title="Crash Into Me")]

    pick = pick_recording(_file("Crash Into Me [Edit]"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.CONFIDENT


def test_a_hit_titled_as_another_version_does_not_match_a_plain_file() -> None:
    hits = [_hit("r1", title="Freaky Girl (Strip mix)", length=211_500)]

    pick = pick_recording(_file("Freaky Girl"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.EMPTY


def test_a_hit_titled_with_the_file_s_own_version_matches() -> None:
    hits = [_hit("r1", title="Freaky Girl (Strip mix)", length=211_500)]

    pick = pick_recording(_file("Freaky Girl [Strip Mix]"), _ARTIST, hits)

    assert pick.outcome is PickOutcome.CONFIDENT
