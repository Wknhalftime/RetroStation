"""Names that hold several MBIDs, and what the Library records about them (phase 2a S13, S14; D2,
D6; C21, M33).

Every MBID of a name holding several (S6's reading) gets an artist, and the name gets one
``shared_name`` suggestion listing the MBIDs and the titles two or more of them share. Works can't
give those titles, because every work stays on the name-only artist until 3b. Nothing merges
automatically: the curator decides, on the page phase 5 adds. Writes in the caller's transaction
and commits nothing (C75).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from backend.domain.catalog import real_mbid
from backend.domain.enums import SuggestionReason
from backend.domain.library import ArtistTagGroup
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.library_suggestions import LibrarySuggestionRepository
from backend.services.normalization import normalize_title


@dataclass(frozen=True)
class SharedName:
    """One name holding several MBIDs: each MBID with the spelling most of its files carry, and
    one spelling of each title files under two or more of the MBIDs hold."""

    normalized_name: str
    spellings: Mapping[str, str]
    shared_titles: tuple[str, ...]


@dataclass(frozen=True)
class SharedNameWrites:
    """What applying one ``SharedName`` wrote."""

    artists_made: int
    row_written: bool


def _most_files(counts: Counter[str]) -> str:
    """The spelling most files carry; of equals, the first in order, so a repeat picks the same."""
    return min(counts, key=lambda spelling: (-counts[spelling], spelling))


def _shared_titles(tagged: Iterable[tuple[str, ArtistTagGroup]]) -> tuple[str, ...]:
    spellings: dict[str, Counter[str]] = {}
    holders: dict[str, set[str]] = {}
    for mbid, group in tagged:
        title = normalize_title(group.track_title) if group.track_title else ""
        if group.track_title and title:
            spellings.setdefault(title, Counter())[group.track_title] += group.files
            holders.setdefault(title, set()).add(mbid)
    return tuple(_most_files(spellings[t]) for t in sorted(holders) if len(holders[t]) > 1)


def _shared_name(normalized_name: str, tagged: list[tuple[str, ArtistTagGroup]]) -> SharedName:
    spellings: dict[str, Counter[str]] = {}
    for mbid, group in tagged:
        spellings.setdefault(mbid, Counter())[group.artist_name] += group.files
    return SharedName(
        normalized_name=normalized_name,
        spellings={mbid: _most_files(counts) for mbid, counts in spellings.items()},
        shared_titles=_shared_titles(tagged),
    )


def plan_shared_names(groups: Iterable[ArtistTagGroup]) -> list[SharedName]:
    """Each name whose tag values hold two or more real MBIDs. A joined, placeholder or malformed
    value is no MBID here (S6's reading), and gets no artist."""
    by_name: dict[str, list[tuple[str, ArtistTagGroup]]] = {}
    for group in groups:
        mbid = real_mbid(group.tag_value)
        if mbid is not None:
            by_name.setdefault(group.normalized_name, []).append((mbid, group))
    return [
        _shared_name(name, tagged)
        for name, tagged in sorted(by_name.items())
        if len({mbid for mbid, _ in tagged}) > 1
    ]


def apply_shared_name(
    shared: SharedName,
    catalog: ArtistCatalogRepository,
    suggestions: LibrarySuggestionRepository,
) -> SharedNameWrites:
    """Write what is missing: an artist for each MBID with none, and the name's row. A known MBID
    is left as it is, and a repeat updates the row's candidates and evidence, never its state."""
    missing = [mbid for mbid in sorted(shared.spellings) if catalog.get_by_mbid(mbid) is None]
    for mbid in missing:
        spelling = shared.spellings[mbid]
        catalog.upsert_musicbrainz_artist(mbid, spelling, spelling, shared.normalized_name)
    row_written = suggestions.upsert_for_name(
        shared.normalized_name,
        SuggestionReason.SHARED_NAME,
        sorted(shared.spellings),
        {"shared_titles": list(shared.shared_titles)},
    )
    return SharedNameWrites(len(missing), row_written)


def record_different_acts(
    suggestions: LibrarySuggestionRepository, normalized_name: str, mbid: str
) -> None:
    """A stub took a name a name-only artist holds (S13, D2): both stay, and the name's one
    ``different_acts`` row lists the stub's MBID beside any earlier stub's. Merge can join them."""
    held = suggestions.get_for_name(normalized_name, SuggestionReason.DIFFERENT_ACTS)
    candidates = {mbid, *(held.candidates if held is not None else ())}
    suggestions.upsert_for_name(
        normalized_name, SuggestionReason.DIFFERENT_ACTS, sorted(candidates), {}
    )
