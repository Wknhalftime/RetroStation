"""The cases of RecordingSearchReachRepository's one question, shared by the fake and Pg tests.

``has_file_beyond_artist_mbid(normalized_artist_name, artist_mbid)`` is true when a PRESENT
file with that normalized artist name carries a recording_mbid and is tagged with the artist
MBID neither as its artist_mbid nor as its album_artist_mbid: the only kind of file song
matching's Step B can add to what Step A already found. Every case asks for ARTIST / MBID.
"""

from __future__ import annotations

from dataclasses import dataclass

ARTIST = "the cure"
MBID = "mb-cure"


@dataclass(frozen=True)
class FileSpec:
    normalized_artist_name: str = ARTIST
    recording_mbid: str | None = "rec-1"
    artist_mbid: str | None = None
    album_artist_mbid: str | None = None
    missing: bool = False


CASES: dict[str, tuple[list[FileSpec], bool]] = {
    "no_files": ([], False),
    "no_recording_mbid": ([FileSpec(recording_mbid=None)], False),
    "no_artist_tags": ([FileSpec()], True),
    "another_artist_mbid": ([FileSpec(artist_mbid="mb-other")], True),
    "another_album_artist_mbid": ([FileSpec(album_artist_mbid="mb-other")], True),
    "tagged_with_the_artist_mbid": ([FileSpec(artist_mbid=MBID)], False),
    "tagged_with_the_album_artist_mbid": ([FileSpec(album_artist_mbid=MBID)], False),
    "album_artist_mbid_only_matches": (
        [FileSpec(artist_mbid="mb-other", album_artist_mbid=MBID)],
        False,
    ),
    "missing_file": ([FileSpec(missing=True)], False),
    "another_artist_name": ([FileSpec(normalized_artist_name="the cure tribute")], False),
    "one_of_several": (
        [FileSpec(artist_mbid=MBID), FileSpec(recording_mbid=None), FileSpec()],
        True,
    ),
}
