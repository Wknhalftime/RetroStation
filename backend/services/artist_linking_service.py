"""Give local catalog artists the MusicBrainz ID their own files carry (AUD-R026; spec D15).

Tags only (Lance, 2026-10-08). A wrong MBID is worse than none: song matching trusts it (Steps
A and B). So an artist links only to an artist MBID its own present files are tagged with, and
only when MusicBrainz's name for it normalizes to the artist's normalized name. MusicBrainz's
special-purpose artists stay local. Untagged artists are not searched by name (deferred, D15b).
Artist matching rules are not touched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from backend.domain.catalog import (
    Artist,
    ArtistLinkCandidate,
    LinkDecision,
    LinkEvidence,
    MusicBrainzId,
)
from backend.domain.enums import ArtistLinkOutcome
from backend.repositories.artist_linking import ArtistLinkingRepository
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.normalization import normalize_artist

# Tagged MBIDs looked up per artist, most present files first. A file credits a few artists.
MAX_TAG_MBIDS = 3

# MusicBrainz's special-purpose artists: placeholders, never a performer an artist can be.
# Source: https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist
# (all twelve checked against it on 2026-10-08; the first four are also on the dev DB). The page
# gives no MBID for [christmas music], [classical music], [soundtrack], [nature sounds] or
# [news report]; a bracketed MusicBrainz name is caught by _is_placeholder_name instead.
SPECIAL_PURPOSE_MBIDS: frozenset[str] = frozenset(
    {
        "89ad4ac3-39f7-470e-963a-56509c546377",  # Various Artists
        "125ec42a-7229-4250-afc5-e057484327fe",  # [unknown]
        "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",  # [no artist]
        "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",  # [dialogue]
        "f731ccc4-e22a-43af-a747-64213329e088",  # [anonymous]
        "9be7f096-97ec-4615-8957-8d40b5dcbc41",  # [traditional]
        "33cf029c-63b0-41a0-9855-be2a3665fb3b",  # [data]
        "a0ef7e1d-44ff-4039-9435-7d5fefdeecc9",  # [theatre]
        "80a8851f-444c-4539-892b-ad2a49292aa9",  # [language instruction]
        "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",  # [Disney]
        "90068d37-bae7-4292-be4a-704c145bd616",  # [church chimes]
        "7e84f845-ac16-41fe-9ff8-df12eb32af55",  # MusicBrainz Test Artist
    }
)

# One tag value may credit several artists: "a, b" (dev DB: 324 such values).
_TAG_SEPARATOR = re.compile(r"[,;/]")


def tag_mbids(tag_counts: Sequence[tuple[str, int]]) -> list[str]:
    """The well-formed MBIDs in the tag values, lowercased, with the most present files first.

    A value holding several MBIDs counts its files for each of them; an MBID's count is summed
    over every value that holds it. Ties are broken by MBID.
    """
    totals: dict[str, int] = {}
    for value, files in tag_counts:
        for part in _TAG_SEPARATOR.split(value):
            mbid = MusicBrainzId.parse(part.strip().lower())
            if mbid is not None:
                totals[mbid.value] = totals.get(mbid.value, 0) + files
    return sorted(totals, key=lambda mbid: (-totals[mbid], mbid))


def decide_link(
    normalized_name: str, evidence: LinkEvidence, mb_client: MusicBrainzClientProtocol
) -> LinkDecision:
    """Decide which tagged MusicBrainz artist, if any, one local artist is (AUD-R026)."""
    tagged = tag_mbids(evidence.tag_counts)
    if not normalized_name or not tagged:
        return LinkDecision(ArtistLinkOutcome.NO_EVIDENCE)
    real = [mbid for mbid in tagged if mbid not in SPECIAL_PURPOSE_MBIDS]
    if not real:
        return LinkDecision(ArtistLinkOutcome.SPECIAL_PURPOSE)
    looked_up = real[:MAX_TAG_MBIDS]
    named: dict[str, ArtistLinkCandidate] = {}
    placeholders = 0
    for mbid in looked_up:
        data = mb_client.lookup_artist(mbid)
        if data is None:
            continue
        name = data.get("name", "")
        if _is_placeholder_name(name):
            placeholders += 1
            continue
        # A merged MBID answers with the surviving artist (the client follows the redirect).
        found = MusicBrainzId.parse(data.get("id", mbid).lower())
        if found is not None and found.value in SPECIAL_PURPOSE_MBIDS:
            placeholders += 1  # a tag merged into a placeholder artist stays local
            continue
        if found is None or normalize_artist(name) != normalized_name:
            continue
        named.setdefault(
            found.value,
            _candidate(found.value, name, data.get("sort-name"), data.get("disambiguation")),
        )
    if len(named) == 1:
        return LinkDecision(ArtistLinkOutcome.LINKED, next(iter(named.values())))
    if named:
        return LinkDecision(ArtistLinkOutcome.AMBIGUOUS)
    if placeholders == len(looked_up):
        return LinkDecision(ArtistLinkOutcome.SPECIAL_PURPOSE)
    return LinkDecision(ArtistLinkOutcome.TAG_MISMATCH)


def link_artist(
    artist: Artist, repo: ArtistLinkingRepository, mb_client: MusicBrainzClientProtocol
) -> ArtistLinkOutcome:
    """Decide one local artist's link and write it; return the outcome written."""
    normalized_name = artist.normalized_name or normalize_artist(artist.name)
    decision = decide_link(normalized_name, repo.evidence(normalized_name), mb_client)
    if decision.candidate is None:
        repo.record_outcome(artist.id, decision.outcome)
        return decision.outcome
    if repo.link(artist.id, decision.candidate):
        return ArtistLinkOutcome.LINKED
    # Another catalog artist already holds the MBID: skip and flag, never merge (AUD-R026).
    repo.record_outcome(artist.id, ArtistLinkOutcome.DUPLICATE)
    return ArtistLinkOutcome.DUPLICATE


def _is_placeholder_name(name: str) -> bool:
    """MusicBrainz names its placeholders in brackets: "[unknown]", "[dialogue]".

    A real artist named in brackets is caught too and stays local: the safe direction.
    """
    return name.startswith("[") and name.endswith("]")


def _candidate(
    mbid: str, name: str, sort_name: str | None, disambiguation: str | None
) -> ArtistLinkCandidate:
    return ArtistLinkCandidate(
        mbid=mbid,
        name=name,
        sort_name=sort_name or name,
        disambiguation=disambiguation or None,
    )
