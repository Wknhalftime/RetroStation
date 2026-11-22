"""Typed shapes for MusicBrainz HTTP API responses.

All field names use the actual MusicBrainz JSON key names (including
hyphens where the API uses them).  Functional TypedDict form is required
for hyphenated keys, which are not valid Python identifiers.
"""

from __future__ import annotations

from typing import TypedDict

# Inner helper types
MbArtistRef = TypedDict(
    "MbArtistRef",
    {"id": str, "name": str, "sort-name": str},
    total=False,
)


class MbArtistCredit(TypedDict, total=False):
    artist: MbArtistRef
    joinphrase: str


class MbWorkRef(TypedDict, total=False):
    id: str
    title: str


class MbRelation(TypedDict, total=False):
    type: str
    attributes: list[str]  # e.g. ["live"] on a live recording's performance relation
    work: MbWorkRef


class MbTrack(TypedDict, total=False):
    recording: MbRecording


class MbMedia(TypedDict, total=False):
    tracks: list[MbTrack]
    position: int
    format: str


# Top-level response shapes returned by mb_client methods

# Returned by search_artist — one element in the "artists" list.
MbArtistResult = TypedDict(
    "MbArtistResult",
    {
        "id": str,
        "name": str,
        "sort-name": str,
        "score": int,
        "disambiguation": str,
    },
    total=False,
)

# Returned by lookup_artist and search_artists_by_mbids
MbArtist = TypedDict(
    "MbArtist",
    {
        "id": str,
        "name": str,
        "sort-name": str,
        "disambiguation": str,
    },
    total=False,
)

# Returned by lookup_release
MbRelease = TypedDict(
    "MbRelease",
    {
        "id": str,
        "title": str,
        "artist-credit": list[MbArtistCredit],
        "media": list[MbMedia],
    },
    total=False,
)


class MbReleaseRef(TypedDict, total=False):
    id: str
    title: str


# One part of a name + title search hit's credit, as the fill caches it (phase 4.1, S2): the
# name as credited, the joining words after it, and the artist's id and name.
class MbCreditPart(TypedDict, total=False):
    name: str
    joinphrase: str
    artist: MbArtistRef


# One hit of search_recordings_by_name_title, slimmed (phase 4.1, S2).
MbNameTitleHit = TypedDict(
    "MbNameTitleHit",
    {
        "id": str,
        "score": int,
        "title": str,
        "length": int,
        "disambiguation": str,
        "artist-credit": list[MbCreditPart],
    },
    total=False,
)


# Returned by search_recordings_by_name_title: the hit count, whether the name was left out
# (no name words, E5), and the hits.
class MbNameTitleResult(TypedDict):
    count: int
    title_only: bool
    recordings: list[MbNameTitleHit]


# One alias of an artist (phase 4.1, D3); MusicBrainz sends null for an unset type, locale or
# primary flag.
MbAlias = TypedDict(
    "MbAlias",
    {
        "name": str,
        "sort-name": str,
        "type": str | None,
        "locale": str | None,
        "primary": bool | None,
    },
)


# Cached by search_artist_aliases_by_mbids under artist-aliases:{mbid}.
class MbArtistAliases(TypedDict):
    id: str
    aliases: list[MbAlias]


# Returned by lookup_recording (with relations) and search_recordings_by_mbids
# (with releases, no relations).
MbRecording = TypedDict(
    "MbRecording",
    {
        "id": str,
        "title": str,
        "disambiguation": str,
        "artist-credit": list[MbArtistCredit],
        "relations": list[MbRelation],
        "releases": list[MbReleaseRef],
        "length": int,
    },
    total=False,
)
