"""Log-name links, read and written in one place (C30, AUD-R035).

``LogNameLinkService`` is the only caller of ``LogNameLinkRepository``
(``tests/test_log_name_links_single_writer.py``). The rules for links live here, never in the
port or its fake, so later phases add rules without changing either. It writes in the caller's
transaction and commits nothing (C75).
"""

from __future__ import annotations

import math
from datetime import datetime
from uuid import UUID

from backend.domain.catalog import Artist
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchTier
from backend.domain.matching import LogNameLink
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.log_name_links import LogNameLinkRepository

# C50: evidence version 0 holds the tier and the score a link was written with.
EVIDENCE_VERSION = 0
_MANUAL_SCORE = 1.0  # what the Resolution Center has always stored for a manual link


def _evidence(tier: MatchTier, score: float) -> dict[str, object]:
    return {"version": EVIDENCE_VERSION, "tier": tier.value, "score": score}


def _strength(score: float) -> int:
    """A match score as a link's strength, 0 to 100, rounded half up as migration 0044 does."""
    return max(0, min(100, math.floor(score + 0.5)))


def _tier1_order(link: LogNameLink) -> tuple[bool, int, datetime, str]:
    """Manual first, then strength (high to low), then the oldest (SPEC 1a S7)."""
    return (link.origin != LogNameLinkOrigin.MANUAL, -link.strength, link.created_at, str(link.id))


class LogNameLinkService:
    """Writes and reads a log name's links to catalog artists."""

    def __init__(self, links: LogNameLinkRepository, catalog: ArtistCatalogRepository) -> None:
        self._links = links
        self._catalog = catalog

    def artist_for_target(self, target: str) -> Artist:
        """The catalog artist a match target names: the one with that id, else the one holding
        that MBID, else a new stub of that MBID (SPEC 1a S3). The MBID's format is not checked:
        a malformed one fails later, when enrichment names the stub."""
        found = self._catalog.get_by_id(target) or self._catalog.get_by_mbid(target)
        return found if found is not None else self._catalog.create_stub(target)

    def link_automatically(
        self, broadcast_artist_id: UUID, target: str, tier: MatchTier, score: float
    ) -> None:
        """The artist matcher's link to the artist *target* names (SPEC 1a S6).

        A pair the curator rejected stays rejected (C19), and a manual link is never replaced
        by an automatic one (C18).
        """
        artist = self.artist_for_target(target)
        links = {link.artist_id: link for link in self._links.list_by_name(broadcast_artist_id)}
        held = links.get(artist.id)
        if held is not None and (
            held.state == LogNameLinkState.REJECTED or held.origin == LogNameLinkOrigin.MANUAL
        ):
            return
        evidence = _evidence(tier, score)
        if held is not None and "legacy" in held.evidence:
            # Keep the copied row (C50): phase 1c's demotion reads it.
            evidence["legacy"] = held.evidence["legacy"]
        self._links.upsert_link(
            LogNameLink(
                broadcast_artist_id=broadcast_artist_id,
                artist_id=artist.id,
                mbid=artist.mbid,
                state=LogNameLinkState.ACTIVE,
                origin=LogNameLinkOrigin.AUTOMATIC,
                strength=_strength(score),
                evidence=evidence,
            )
        )

    def link_by_hand(self, broadcast_artist_id: UUID, target: str) -> None:
        """The curator's link to the artist *target* names (SPEC 1a S9): one manual link per
        pair, whatever the pair held before; the name's other links stay."""
        artist = self.artist_for_target(target)
        self._links.upsert_link(
            LogNameLink(
                broadcast_artist_id=broadcast_artist_id,
                artist_id=artist.id,
                mbid=artist.mbid,
                state=LogNameLinkState.ACTIVE,
                origin=LogNameLinkOrigin.MANUAL,
                strength=100,
                evidence=_evidence(MatchTier.MANUAL, _MANUAL_SCORE),
            )
        )

    def reject_all(self, broadcast_artist_id: UUID) -> None:
        """Remember every link of the log name as rejected (C19, SPEC 1b S2, S3, E5): Reject and
        Unmatch keep the pairs so the matcher never adds them back. A link already rejected is
        left as it is."""
        for link in self._links.list_by_name(broadcast_artist_id):
            if link.state != LogNameLinkState.REJECTED:
                self._links.set_link_state(
                    broadcast_artist_id, link.artist_id, LogNameLinkState.REJECTED
                )

    def rejected_artist(self, broadcast_artist_id: UUID, target: str) -> Artist | None:
        """The catalog artist *target* names, when the log name's link to it is rejected; None
        otherwise (SPEC 1b S4). Makes no stub: an artist with no catalog row has no link.
        Reads the name's links first: most names have no rejected link, and then the catalog
        is not read."""
        rejected = {
            link.artist_id
            for link in self._links.list_by_name(broadcast_artist_id)
            if link.state == LogNameLinkState.REJECTED
        }
        if not rejected:
            return None
        artist = self._catalog.get_by_id(target) or self._catalog.get_by_mbid(target)
        return artist if artist is not None and artist.id in rejected else None

    def unlink_all(self, broadcast_artist_id: UUID) -> None:
        """Remove every link of the log name, in every state (SPEC 1a S10).

        No caller since phase 1b, whose Unmatch rejects the links instead (C19); it stays
        because the locked ``tests/test_log_name_links_single_writer.py`` expects the service
        to own the port's delete."""
        self._links.delete_links_of_name(broadcast_artist_id)

    def tier1_link(self, broadcast_artist_id: UUID) -> LogNameLink | None:
        """The link song matching's Tier 1 uses: the first active one, manual first, then the
        strongest, then the oldest (SPEC 1a S7); None when the name has no active link."""
        active = [
            link
            for link in self._links.list_by_name(broadcast_artist_id)
            if link.state == LogNameLinkState.ACTIVE
        ]
        return min(active, key=_tier1_order, default=None)

    def is_linked(self, artist_id: str) -> bool:
        """Whether a link in any state points at the artist (SPEC 1a S11, AUD-R035)."""
        return bool(self._links.list_by_artist(artist_id))
