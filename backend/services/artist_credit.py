"""Pairing a file's artist names with its MBIDs (phase 2a S4, E2; C69, M1, M32; AUD-R041).

Pure: no repository and no lookup, so the scan can use it and the backfill can add to what it
gives. MBIDs split on the MBID pattern, and names are never split. By position when the counts
agree, a value that holds no MBID keeping its place; otherwise the names stay null for MusicBrainz
to give. Joining words come from locating each name, in order, in the printed ``artist``, and where
a name isn't there, from a MusicBrainz credit if it has any.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from backend.domain.catalog import find_mbids
from backend.domain.library import CreditEntry


def split_mbids(values: Iterable[str]) -> tuple[str, ...]:
    """Every MBID the values hold, lowercased, each once, at its first position (E2)."""
    return tuple(dict.fromkeys(mbid for value in values for mbid in find_mbids(value)))


def _positions(values: Iterable[str]) -> list[str | None]:
    """Each MBID in its place. A value holding none keeps its place as None: position is what
    pairs, so dropping it would shift every name after it onto the wrong MBID."""
    positions: list[str | None] = []
    for value in values:
        positions.extend(find_mbids(value) or (None,))
    return positions


def _clean(text: str | None) -> str | None:
    """As the reader cleans a scalar tag, and without the NUL that JSONB refuses."""
    return (text.replace("\x00", "").strip() or None) if text is not None else None


def _named_by_position(
    names: Sequence[str | None], positions: Sequence[str | None]
) -> dict[str, str | None] | None:
    """Each MBID's name when the counts agree, doubled tags included; None when they can't."""
    if len(names) != len(positions):
        positions = list(dict.fromkeys(positions))
    if len(names) != len(positions):
        return None
    named: dict[str, str | None] = {}
    for mbid, name in zip(positions, names, strict=True):
        if mbid is not None:
            named.setdefault(mbid, name)
    return named


def _spans(artist: str, entries: Sequence[CreditEntry]) -> list[tuple[int, int] | None]:
    """Where each name sits in the print, each searched for after the one before it."""
    spans: list[tuple[int, int] | None] = []
    position = 0
    for entry in entries:
        start = artist.find(entry.name, position) if entry.name else -1
        if start < 0:
            spans.append(None)
            continue
        position = start + len(entry.name or "")
        spans.append((start, position))
    return spans


def joins_from_print(artist: str | None, entries: Sequence[CreditEntry]) -> tuple[CreditEntry, ...]:
    """*entries* with the joining words the print gives. Where it gives none, an entry keeps what
    it held: a name that isn't there (a credited-as name) has no span, and neither has the join
    touching it."""
    if artist is None:
        return tuple(entries)
    spans = _spans(artist, entries)
    joined: list[CreditEntry] = []
    for index, (entry, span) in enumerate(zip(entries, spans, strict=True)):
        following = spans[index + 1] if index + 1 < len(spans) else (len(artist), len(artist))
        if span is None or following is None:
            joined.append(entry)
        else:
            joined.append(replace(entry, join=artist[span[1] : following[0]]))
    return tuple(joined)


def pair_credit(
    artist: str | None, names: Sequence[str], values: Sequence[str]
) -> tuple[CreditEntry, ...] | None:
    """A file's credit from its ``artist``, ``artists`` and artist-MBID tags; None for no MBID. A
    single MBID pairs with the whole text. Otherwise by position, or with null names when the
    counts differ, for MusicBrainz to give."""
    mbids = split_mbids(values)
    if not mbids:
        return None
    printed = _clean(artist)
    positions = _positions(values)
    if len(set(positions)) == 1:
        return (CreditEntry(mbids[0], printed, "" if printed is not None else None),)
    named = _named_by_position([_clean(name) for name in names], positions)
    if named is None:
        return tuple(CreditEntry(mbid) for mbid in mbids)
    return joins_from_print(printed, [CreditEntry(mbid, named[mbid]) for mbid in mbids])


def tag_values(raw: object) -> list[str]:
    """The values of one ``raw_metadata`` entry, which stores ``str(value)``: a Vorbis list
    arrives as its printed form, a NUL escaped past the scan's sanitising."""
    if isinstance(raw, list):
        return [str(value).replace("\x00", "") for value in raw]
    if not isinstance(raw, str) or not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return [raw]
        if isinstance(parsed, list):
            return [str(value).replace("\x00", "") for value in parsed]
    return [raw]


def credit_from_raw_metadata(
    raw: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[CreditEntry, ...] | None]:
    """A row's ``artist_mbids`` and ``credit`` from its saved Vorbis tags (C26)."""
    values = tag_values(raw.get("musicbrainz_artistid"))
    printed = tag_values(raw.get("artist"))
    credit = pair_credit(printed[0] if printed else None, tag_values(raw.get("artists")), values)
    return split_mbids(values), credit


def _credited(credit: Sequence[Mapping[str, object]]) -> list[tuple[str, str | None, str | None]]:
    """(MBID, the artist's name, joining words) of each credit that names an artist. A credit's
    own name can be a credited-as one, and the artist's is what names the MBID."""
    found: list[tuple[str, str | None, str | None]] = []
    for entry in credit:
        artist = entry.get("artist")
        if not isinstance(artist, Mapping) or not artist.get("id"):
            continue
        name = artist.get("name")
        join = entry.get("joinphrase")
        found.append(
            (
                str(artist["id"]).lower(),
                str(name) if name else None,
                str(join) if join is not None else None,
            )
        )
    return found


def name_from_musicbrainz(
    entries: Sequence[CreditEntry],
    cached_artists: Mapping[str, str],
    credit: Sequence[Mapping[str, object]] | None,
) -> tuple[CreditEntry, ...]:
    """*entries* with the names MusicBrainz gives for their own MBIDs: a cached artist first, then
    the credit. A name the tags gave stands, and a credited stranger adds nothing (M1). A credit's
    joining words count only when it credits exactly these artists in this order: a join next to
    a stranger isn't this file's."""
    credited = _credited(credit or ())
    names = {mbid: name for mbid, name, _ in credited if name}
    same_artists = [mbid for mbid, _, _ in credited] == [entry.mbid for entry in entries]
    joins = {mbid: join for mbid, _, join in credited} if same_artists else {}
    return tuple(
        CreditEntry(
            entry.mbid,
            entry.name or cached_artists.get(entry.mbid) or names.get(entry.mbid),
            entry.join if entry.join is not None else joins.get(entry.mbid),
        )
        for entry in entries
    )
