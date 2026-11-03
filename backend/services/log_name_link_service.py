"""Log-name links, read and written in one place (C30, AUD-R035).

``LogNameLinkService`` is the only caller of ``LogNameLinkRepository``
(``tests/test_log_name_links_single_writer.py``). The rules for links live here, never in the
port or its fake, so later phases add rules without changing either. It writes in the caller's
transaction and commits nothing (C75).

From phase 1c (S3, C36) it also writes the log name's status, in the same transaction as the
link change, by ``status_from_links`` (``domain/log_name_status.py``): the matcher's answer
(guarded on pending, AUD-R018), Resolve and Reject. Unmatch's decided-only write stays in the
resolution repository, as the guard behind its 409.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from backend.domain.catalog import Artist
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchStatus, MatchTier
from backend.domain.log_name_status import NameState, status_from_links
from backend.domain.matching import LogNameLink
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.services.mb_types import MbArtistResult
from backend.services.normalization import normalize_artist

# C50: evidence version 0 holds the tier and the score a link was written with.
EVIDENCE_VERSION = 0
_MANUAL_SCORE = 1.0  # what the Resolution Center has always stored for a manual link
_PENDING = NameState(MatchStatus.PENDING)


def _evidence(tier: MatchTier, score: float) -> dict[str, object]:
    return {"version": EVIDENCE_VERSION, "tier": tier.value, "score": score}


def _strength(score: float) -> int:
    """A match score as a link's strength, 0 to 100, rounded half up as migration 0044 does."""
    return max(0, min(100, math.floor(score + 0.5)))


def _tier1_order(link: LogNameLink) -> tuple[bool, int, datetime, str]:
    """Manual first, then strength (high to low), then the oldest (SPEC 1a S7)."""
    return (link.origin != LogNameLinkOrigin.MANUAL, -link.strength, link.created_at, str(link.id))


@dataclass(frozen=True)
class LinkTarget:
    """An artist a matcher answer names: its target (an MBID or an ``artists.id``), the tier and
    the score, and the MusicBrainz hit the catalog takes first (the MusicBrainz tier only)."""

    target: str
    tier: MatchTier
    score: float
    mb_candidate: MbArtistResult | None = None


@dataclass(frozen=True)
class MatcherAnswer:
    """What the artist matcher answers for one pending name (phase 1c S4): the status it names,
    the artist it links (95+ or an exact name with one artist), or its suggestions."""

    proposed: NameState
    link: LinkTarget | None = None
    suggestions: tuple[LinkTarget, ...] = ()


class _NoLinkStore(LogNameLinkRepository):
    """Holds no link and keeps none: a matcher run built without a link store (tests only; the
    tasks always pass one, tests/services/test_matching_repos_wiring.py)."""

    def list_by_name(self, broadcast_artist_id: UUID) -> list[LogNameLink]:
        return []

    def list_by_artist(self, artist_id: str) -> list[LogNameLink]:
        return []

    def upsert_link(self, link: LogNameLink) -> LogNameLink:
        return link

    def set_link_state(
        self, broadcast_artist_id: UUID, artist_id: str, state: LogNameLinkState
    ) -> bool:
        return False

    def delete_links_of_name(self, broadcast_artist_id: UUID) -> int:
        return 0


NO_LINK_STORE: LogNameLinkRepository = _NoLinkStore()


class LogNameLinkService:
    """Writes and reads a log name's links to catalog artists, and the status they give it.

    ``names`` writes the status (phase 1c S3); without it the service writes links only, as the
    readers and the locked 1a link tests build it.
    """

    def __init__(
        self,
        links: LogNameLinkRepository,
        catalog: ArtistCatalogRepository,
        names: BroadcastArtistRepository | None = None,
    ) -> None:
        self._links = links
        self._catalog = catalog
        self._names = names

    def artist_for_target(self, target: str) -> Artist:
        """The catalog artist a match target names: the one with that id, else the one holding
        that MBID, else a new stub of that MBID (SPEC 1a S3). The MBID's format is not checked:
        a malformed one fails later, when enrichment names the stub."""
        found = self._known(target)
        return found if found is not None else self._catalog.create_stub(target)

    def _known(self, target: str) -> Artist | None:
        return self._catalog.get_by_id(target) or self._catalog.get_by_mbid(target)

    # --- the matcher (phase 1c S3, S4) ------------------------------------------------------

    def _plan(
        self, broadcast_artist_id: UUID, answer: MatcherAnswer
    ) -> tuple[list[LogNameLink], list[tuple[LinkTarget, LogNameLinkState]]]:
        """The links the name holds once *answer* is written, and the writes that get there.
        Reads only: an artist the catalog does not hold yet is a new pair. A rejected or manual
        pair is never touched (C18, C19), and a suggestion never demotes or rewrites a pair the
        name holds."""
        held = {link.artist_id: link for link in self._links.list_by_name(broadcast_artist_id)}
        after: dict[str, LogNameLink] = dict(held)
        writes: list[tuple[LinkTarget, LogNameLinkState]] = []
        wanted = [(answer.link, LogNameLinkState.ACTIVE)] if answer.link is not None else []
        wanted += [(target, LogNameLinkState.SUGGESTED) for target in answer.suggestions]
        for target, state in wanted:
            artist = self._known(target.target)
            key = artist.id if artist is not None else f"new:{target.target}"
            pair = after.get(key)
            if pair is not None and (
                pair.state == LogNameLinkState.REJECTED
                or pair.origin == LogNameLinkOrigin.MANUAL
                or state == LogNameLinkState.SUGGESTED
            ):
                continue
            after[key] = LogNameLink(
                broadcast_artist_id=broadcast_artist_id,
                artist_id=key,
                state=state,
                origin=LogNameLinkOrigin.AUTOMATIC,
                strength=_strength(target.score),
                evidence=_evidence(target.tier, target.score),
            )
            writes.append((target, state))
        return list(after.values()), writes

    def answer_state(self, broadcast_artist_id: UUID, answer: MatcherAnswer) -> NameState:
        """The status *answer* gives the pending name: S3's rule over the links it will hold.
        Reads only."""
        after, _ = self._plan(broadcast_artist_id, answer)
        return status_from_links(_PENDING, after, answer.proposed)

    def write_answer(self, broadcast_artist_id: UUID, answer: MatcherAnswer) -> bool:
        """Write the matcher's *answer*: the name's status first, guarded on the pending status
        the run read (AUD-R018), then, only if it was written, each MusicBrainz hit into the
        catalog and each link. Whether it was written."""
        if self._names is None:
            raise RuntimeError("the matcher's answer writes the name's status: pass names")
        after, writes = self._plan(broadcast_artist_id, answer)
        state = status_from_links(_PENDING, after, answer.proposed)
        if not self._names.update_match_status_if_pending(
            broadcast_artist_id,
            state.status,
            reason_code=state.reason_code,
            reason_detail=state.reason_detail,
        ):
            return False
        for target, link_state in writes:
            if target.mb_candidate is not None:
                self._upsert_musicbrainz_artist(target.mb_candidate)
            if link_state == LogNameLinkState.ACTIVE:
                self.link_automatically(
                    broadcast_artist_id, target.target, target.tier, target.score
                )
            else:
                self._suggest(broadcast_artist_id, target)
        return True

    def _upsert_musicbrainz_artist(self, hit: MbArtistResult) -> None:
        """The catalog artist of a MusicBrainz hit, before its link: the link then names the
        artist, not a stub made for a link written first (1a S6)."""
        self._catalog.upsert_musicbrainz_artist(
            mbid=hit["id"],
            name=hit["name"],
            sort_name=hit.get("sort-name", hit["name"]),
            normalized_name=normalize_artist(hit["name"]),
            disambiguation=hit.get("disambiguation"),
        )

    def _suggest(self, broadcast_artist_id: UUID, target: LinkTarget) -> None:
        """A suggested link to the artist *target* names (phase 1c S4), unless the name already
        holds the pair in any state."""
        artist = self.artist_for_target(target.target)
        if any(
            link.artist_id == artist.id for link in self._links.list_by_name(broadcast_artist_id)
        ):
            return
        self._links.upsert_link(
            LogNameLink(
                broadcast_artist_id=broadcast_artist_id,
                artist_id=artist.id,
                mbid=artist.mbid,
                state=LogNameLinkState.SUGGESTED,
                origin=LogNameLinkOrigin.AUTOMATIC,
                strength=_strength(target.score),
                evidence=_evidence(target.tier, target.score),
            )
        )

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

    # --- the curator (phase 1a S9, 1b, 1c S3) -------------------------------------------------

    def _write_by_hand(self, broadcast_artist_id: UUID, action: NameState) -> None:
        """The status a user action names (S3's first rule), its old machine reason cleared."""
        if self._names is None:
            return
        state = status_from_links(action, (), action, by_user=True)
        self._names.update_match_status(
            broadcast_artist_id, state.status, state.reason_code, state.reason_detail
        )

    def link_by_hand(self, broadcast_artist_id: UUID, target: str) -> None:
        """The curator's link to the artist *target* names (SPEC 1a S9): the name
        ``manual_matched``, and one manual link per pair, whatever the pair held before; the
        name's other links stay. The name's row first, as the matcher writes it."""
        self._write_by_hand(broadcast_artist_id, NameState(MatchStatus.MANUAL_MATCHED))
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

    def reject_by_hand(self, broadcast_artist_id: UUID) -> None:
        """The curator's Reject of the whole name: ``manual_rejected``, and every link rejected
        (C19, SPEC 1b S2)."""
        self._write_by_hand(broadcast_artist_id, NameState(MatchStatus.MANUAL_REJECTED))
        self.reject_all(broadcast_artist_id)

    def reject_all(self, broadcast_artist_id: UUID) -> None:
        """Remember every link of the log name as rejected (C19, SPEC 1b S2, S3, E5): Reject and
        Unmatch keep the pairs so the matcher never adds them back. A link already rejected is
        left as it is."""
        for link in self._links.list_by_name(broadcast_artist_id):
            if link.state != LogNameLinkState.REJECTED:
                self._links.set_link_state(
                    broadcast_artist_id, link.artist_id, LogNameLinkState.REJECTED
                )

    # --- readers ------------------------------------------------------------------------------

    def rejected_artist(self, broadcast_artist_id: UUID, target: str) -> Artist | None:
        """The catalog artist *target* names, when the log name's link to it is rejected; None
        otherwise (SPEC 1b S4). Makes no stub: an artist with no catalog row has no link.
        Reads the name's links first: most names have no rejected link, and then the catalog
        is not read."""
        rejected = self._rejected_pairs(broadcast_artist_id)
        if not rejected:
            return None
        artist = self._known(target)
        return artist if artist is not None and artist.id in rejected else None

    def rejected_artists(
        self, broadcast_artist_id: UUID, targets: Sequence[str]
    ) -> dict[str, Artist]:
        """The rejected catalog artist each of *targets* names, by target; the others are left
        out (phase 1c S4's suggestions). One read of the name's links."""
        rejected = self._rejected_pairs(broadcast_artist_id)
        if not rejected:
            return {}
        found = {target: self._known(target) for target in targets}
        return {
            target: artist
            for target, artist in found.items()
            if artist is not None and artist.id in rejected
        }

    def _rejected_pairs(self, broadcast_artist_id: UUID) -> set[str]:
        return {
            link.artist_id
            for link in self._links.list_by_name(broadcast_artist_id)
            if link.state == LogNameLinkState.REJECTED
        }

    def unlink_all(self, broadcast_artist_id: UUID) -> None:
        """Remove every link of the log name, in every state (SPEC 1a S10).

        No caller since phase 1b, whose Unmatch rejects the links instead (C19); it stays
        because the locked ``tests/test_log_name_links_single_writer.py`` expects the service
        to own the port's delete (it retires in phase 1c's S12)."""
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
