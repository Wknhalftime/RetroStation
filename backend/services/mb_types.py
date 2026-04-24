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

# Nested in lookup_artist response under "aliases"
# MB API returns JSON booleans for "primary" (not strings); typing as
# `str` would make truthiness checks always True on non-empty values.
MbAliasEntry = TypedDict(
    "MbAliasEntry",
    {
        "name": str,
        "sort-name": str,
        "locale": str,
        "type": str,
        "primary": bool,
    },
    total=False,
)


# Nested in lookup_artist response under "tags"
class MbTagEntry(TypedDict, total=False):
    name: str
    count: int


# Returned by lookup_artist
MbArtist = TypedDict(
    "MbArtist",
    {
        "id": str,
        "name": str,
        "sort-name": str,
        "disambiguation": str,
        "aliases": list[MbAliasEntry],
        "tags": list[MbTagEntry],
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

# Returned by lookup_recording
MbRecording = TypedDict(
    "MbRecording",
    {
        "id": str,
        "title": str,
        "artist-credit": list[MbArtistCredit],
        "relations": list[MbRelation],
        "length": int,
    },
    total=False,
)
