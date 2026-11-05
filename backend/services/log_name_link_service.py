"""Log-name links, read and written in one place (C30, AUD-R035).

``LogNameLinkService`` is the only caller of ``LogNameLinkRepository``
(``tests/test_log_name_links_single_writer.py``). The rules for links live here, never in the
port or its fake, so later phases add rules without changing either. It writes in the caller's
transaction and commits nothing (C75).

From phase 1c (S3, C36) it also writes the log name's status, in the same transaction as the
link change, by ``status_from_links`` (``domain/log_name_status.py``): the matcher's answer
(guarded on pending, AUD-R018), Resolve and Reject. Unmatch's decided-only write stays in the
resolution repository, as the guard behind its 409.

Placeholders are never linked (C7, phase 1c S5, S6): no link of any state names one of
MusicBrainz's special-purpose artists (``SPECIAL_PURPOSE_MBIDS``), and a log name on the
not-an-artist list is never linked by hand.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS, Artist
from backend.domain.enums import (
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    ReasonCode,
)
from backend.domain.log_name_status import NameState, status_from_links
from backend.domain.matching import (
    ListedLogNameError,
    LogNameLink,
    LogNameNotFoundError,
    PlaceholderArtistError,
)
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.repositories.log_name_placeholders import LogNamePlaceholderRepository
from backend.services.matching_constants import MAX_ARTIST_SUGGESTIONS
from backend.services.matching_reasons import format_placeholder_refused
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


@dataclass(frozen=True)
class AnswerPlan:
    """What writing a matcher answer does: the status it gives the name, and the links it
    writes, each with the state it gets (planned once, written once)."""

    state: NameState
    writes: tuple[tuple[LinkTarget, LogNameLinkState], ...] = ()


def _wanted(answer: MatcherAnswer) -> list[tuple[LinkTarget, LogNameLinkState]]:
    wanted = [(answer.link, LogNameLinkState.ACTIVE)] if answer.link is not None else []
    return wanted + [(target, LogNameLinkState.SUGGESTED) for target in answer.suggestions]


def _suggested(links: dict[str, LogNameLink]) -> int:
    return sum(1 for link in links.values() if link.state == LogNameLinkState.SUGGESTED)


def _on_a_rejected_name(
    target: LinkTarget, artist: Artist | None, rejected_names: dict[str, str]
) -> bool:
    """Whether *target* resolves to a different artist with the normalized name of one the log
    name rejected (phase 2a D7): whichever row was rejected, the curator rejected that name. The
    rejected artist itself is no such target: its pair stays refused, never suggested back."""
    if artist is not None and artist.id in rejected_names:
        return False
    names: set[str | None] = {artist.normalized_name} if artist is not None else set()
    if target.mb_candidate is not None:
        names.add(normalize_artist(target.mb_candidate["name"]))
    return not names.isdisjoint(rejected_names.values())


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
    readers and the locked 1a link tests build it. ``placeholders`` is the not-an-artist list
    (phase 1c S6); without it no name is listed.
    """

    def __init__(
        self,
        links: LogNameLinkRepository,
        catalog: ArtistCatalogRepository,
        names: BroadcastArtistRepository | None = None,
        placeholders: LogNamePlaceholderRepository | None = None,
    ) -> None:
        self._links = links
        self._catalog = catalog
        self._names = names
        self._placeholders = placeholders

    def artist_for_target(self, target: str) -> Artist:
        """The catalog artist a match target names: the one with that id, else the one holding
        that MBID, else a new stub of that MBID (SPEC 1a S3). The MBID's format is not checked:
        a malformed one fails later, when enrichment names the stub."""
        found = self._known(target)
        return found if found is not None else self._catalog.create_stub(target)

    def _known(self, target: str) -> Artist | None:
        return self._catalog.get_by_id(target) or self._catalog.get_by_mbid(target)

    def placeholder_named(self, target: str, hit: MbArtistResult | None = None) -> str | None:
        """The name of the special-purpose artist *target* names (C7, phase 1c S5): its MBID,
        or the catalog artist holding one; None for any other target. Makes no stub."""
        if target in SPECIAL_PURPOSE_MBIDS:
            known = self._catalog.get_by_mbid(target)
            if known is not None:
                return known.name
            return hit["name"] if hit is not None else "a placeholder artist"
        known = self._catalog.get_by_id(target)
        if known is not None and known.mbid in SPECIAL_PURPOSE_MBIDS:
            return known.name
        return None

    def placeholders_in(self, answer: MatcherAnswer) -> dict[str, str]:
        """The special-purpose artist each target of *answer* names, by target (E3); a target
        that names a performer is left out."""
        found = {
            target.target: self.placeholder_named(target.target, target.mb_candidate)
            for target, _ in _wanted(answer)
        }
        return {target: name for target, name in found.items() if name is not None}

    # --- the matcher (phase 1c S3, S4) ------------------------------------------------------

    def _resolve(self, target: LinkTarget, state: LogNameLinkState) -> Artist | None:
        """The catalog artist the write will link, read before anything is written; None for a
        new pair. A MusicBrainz hit of an automatic match is resolved as its upsert will (phase
        2a S6): its MBID's artist, else the name-only artist of its name when the name's MBIDs
        are none or only this one, which the upsert gives the MBID. A suggestion's hit never
        gives a local artist its MBID (``_upsert_suggested``), so only its MBID's artist
        counts."""
        hit = target.mb_candidate
        if hit is None:
            return self._known(target.target)
        found = self._catalog.get_by_mbid(target.target)
        if found is not None or state != LogNameLinkState.ACTIVE:
            return found
        normalized = normalize_artist(hit["name"])
        holder = self._catalog.get_by_normalized_name(normalized)
        if holder is None or not self._catalog.name_mbids(normalized) <= {hit["id"]}:
            return None
        return holder

    def rejected_in(self, broadcast_artist_id: UUID, answer: MatcherAnswer) -> dict[str, Artist]:
        """The rejected catalog artist each target of *answer* names, by target (SPEC 1b S4, 1c
        E3); a target whose pair is not rejected is left out. Reads the name's links first:
        most names have no rejected link, and then the catalog is not read."""
        rejected = self._rejected_pairs(broadcast_artist_id)
        if not rejected:
            return {}
        found = {target.target: self._resolve(target, state) for target, state in _wanted(answer)}
        return {
            target: artist
            for target, artist in found.items()
            if artist is not None and artist.id in rejected
        }

    def plan_answer(self, broadcast_artist_id: UUID, answer: MatcherAnswer) -> AnswerPlan:
        """What writing *answer* does, and the status it gives the pending name: S3's rule over
        the links it will hold. Reads only. A rejected or manual pair is never touched (C18,
        C19), a suggestion never demotes or rewrites a pair the name holds, and a name holds at
        most MAX_ARTIST_SUGGESTIONS suggestions (AUD-R038). A special-purpose artist is never
        planned (C7, S5): when the answer named only such artists, the name waits in review
        with NO_CANDIDATES, never matched with no link. An automatic match on a name the log name
        rejected is planned as a suggestion, so the name stays in review (phase 2a D7)."""
        refused = self.placeholders_in(answer)
        wanted = [(t, s) for t, s in _wanted(answer) if t.target not in refused]
        proposed = answer.proposed
        if refused and not wanted:
            proposed = NameState(
                MatchStatus.NEEDS_REVIEW,
                ReasonCode.NO_CANDIDATES,
                format_placeholder_refused(next(iter(refused.values()))),
            )
        held = {link.artist_id: link for link in self._links.list_by_name(broadcast_artist_id)}
        after: dict[str, LogNameLink] = dict(held)
        writes: list[tuple[LinkTarget, LogNameLinkState]] = []
        rejected = self._rejected_names(held.values())
        for target, state in wanted:
            artist = self._resolve(target, state)
            if state == LogNameLinkState.ACTIVE and _on_a_rejected_name(target, artist, rejected):
                state = LogNameLinkState.SUGGESTED
                artist = self._resolve(target, state)
            key = artist.id if artist is not None else f"new:{target.target}"
            pair = after.get(key)
            if pair is not None and (
                pair.state == LogNameLinkState.REJECTED
                or pair.origin == LogNameLinkOrigin.MANUAL
                or state == LogNameLinkState.SUGGESTED
            ):
                continue
            if state == LogNameLinkState.SUGGESTED and _suggested(after) >= MAX_ARTIST_SUGGESTIONS:
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
        state_after = status_from_links(_PENDING, after.values(), proposed)
        return AnswerPlan(state=state_after, writes=tuple(writes))

    def _rejected_names(self, links: Iterable[LogNameLink]) -> dict[str, str]:
        """The normalized name of each artist the log name rejected, by artist id (D7). A stub
        with no name yet has none to protect until enrichment names it. Reads the catalog only
        for a rejected pair: most names hold none."""
        rejected = (
            self._catalog.get_by_id(link.artist_id)
            for link in links
            if link.state == LogNameLinkState.REJECTED
        )
        return {
            artist.id: artist.normalized_name
            for artist in rejected
            if artist is not None and artist.normalized_name
        }

    def write_plan(self, broadcast_artist_id: UUID, plan: AnswerPlan) -> bool:
        """Write a matcher answer's *plan*: the name's status first, guarded on the pending
        status the run read (AUD-R018), then, only if it was written, each MusicBrainz hit into
        the catalog and each link. Whether it was written."""
        if self._names is None:
            raise RuntimeError("the matcher's answer writes the name's status: pass names")
        if not self._names.update_match_status_if_pending(
            broadcast_artist_id,
            plan.state.status,
            reason_code=plan.state.reason_code,
            reason_detail=plan.state.reason_detail,
        ):
            return False
        for target, link_state in plan.writes:
            if link_state == LogNameLinkState.ACTIVE:
                if target.mb_candidate is not None:
                    self._upsert_musicbrainz_artist(target.mb_candidate)
                self.link_automatically(
                    broadcast_artist_id, target.target, target.tier, target.score
                )
            else:
                if target.mb_candidate is not None:
                    self._upsert_suggested(target.mb_candidate)
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

    def _upsert_suggested(self, hit: MbArtistResult) -> None:
        """A suggested MusicBrainz hit's artist, upserted unless the upsert would give its MBID
        to a local-only artist of its name: an unconfirmed suggestion never changes a local
        artist (the suggestion then names a stub, and its evidence keeps the hit's name)."""
        if (
            self._catalog.get_by_mbid(hit["id"]) is None
            and self._catalog.get_by_normalized_name(normalize_artist(hit["name"])) is not None
        ):
            return
        self._upsert_musicbrainz_artist(hit)

    def _suggest(self, broadcast_artist_id: UUID, target: LinkTarget) -> None:
        """A suggested link to the artist *target* names (phase 1c S4), unless the name already
        holds the pair in any state, or it is a special-purpose artist (S5). A MusicBrainz hit's
        name and disambiguation go into its evidence (C30: the target as it was), for a stub
        enrichment has not named yet."""
        if target.target in SPECIAL_PURPOSE_MBIDS:
            return
        artist = self.artist_for_target(target.target)
        if artist.mbid in SPECIAL_PURPOSE_MBIDS:
            return
        if any(
            link.artist_id == artist.id for link in self._links.list_by_name(broadcast_artist_id)
        ):
            return
        evidence = _evidence(target.tier, target.score)
        if target.mb_candidate is not None:
            evidence["target_name"] = target.mb_candidate["name"]
            evidence["target_disambiguation"] = target.mb_candidate.get("disambiguation")
        self._links.upsert_link(
            LogNameLink(
                broadcast_artist_id=broadcast_artist_id,
                artist_id=artist.id,
                mbid=artist.mbid,
                state=LogNameLinkState.SUGGESTED,
                origin=LogNameLinkOrigin.AUTOMATIC,
                strength=_strength(target.score),
                evidence=evidence,
            )
        )

    def link_automatically(
        self, broadcast_artist_id: UUID, target: str, tier: MatchTier, score: float
    ) -> None:
        """The artist matcher's link to the artist *target* names (SPEC 1a S6).

        A pair the curator rejected stays rejected (C19), a manual link is never replaced by an
        automatic one (C18), and a special-purpose artist is never linked, nor its stub made
        (C7, phase 1c S5).
        """
        if target in SPECIAL_PURPOSE_MBIDS:
            return
        artist = self.artist_for_target(target)
        if artist.mbid in SPECIAL_PURPOSE_MBIDS:
            return
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
        name's other links stay. The name's row first, as the matcher writes it.

        Raises (before anything is written):
            ListedLogNameError: the name is on the not-an-artist list (phase 1c S6).
            PlaceholderArtistError: *target* names a special-purpose artist (phase 1c S5).
        """
        if self._is_listed(broadcast_artist_id):
            raise ListedLogNameError(f"Artist {broadcast_artist_id} is on the not-an-artist list")
        placeholder = self.placeholder_named(target)
        if placeholder is not None:
            raise PlaceholderArtistError(f"{placeholder} is a placeholder, never a performer")
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

    # --- the not-an-artist list (phase 1c S6) -------------------------------------------------

    def unlink_keeping_rejected(self, broadcast_artist_id: UUID) -> None:
        """Remove the log name's active and suggested links; a pair the curator rejected stays
        rejected (C19; Lance, 2026-11-03), so no later run links or suggests it. The port has
        no one-link delete: every link goes, and each rejected one is written back as it was
        (its id, ``created_at``, strength and evidence)."""
        held = self._links.list_by_name(broadcast_artist_id)
        if all(link.state == LogNameLinkState.REJECTED for link in held):
            return
        self._links.delete_links_of_name(broadcast_artist_id)
        for link in held:
            if link.state == LogNameLinkState.REJECTED:
                self._links.upsert_link(link)

    def write_status_from_links(self, broadcast_artist_id: UUID) -> None:
        """The status S3's rule gives the name's links, with no proposal: a matched name with
        no link goes to review with LINK_REMOVED; any other keeps what its last writer set."""
        if self._names is None:
            raise RuntimeError("the status is written to the names: pass names")
        name = self._names.get_by_id(broadcast_artist_id)
        if name is None:
            raise LogNameNotFoundError(f"Artist {broadcast_artist_id} not found")
        current = NameState(name.match_status, name.reason_code, name.reason_detail)
        state = status_from_links(current, self._links.list_by_name(broadcast_artist_id))
        if state != current:
            self._names.update_match_status(
                broadcast_artist_id, state.status, state.reason_code, state.reason_detail
            )

    def _is_listed(self, broadcast_artist_id: UUID) -> bool:
        if self._placeholders is None or self._names is None:
            return False
        name = self._names.get_by_id(broadcast_artist_id)
        if name is None:
            return False
        return bool(self._placeholders.listed({name.normalized_name}))

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

    def _rejected_pairs(self, broadcast_artist_id: UUID) -> set[str]:
        return {
            link.artist_id
            for link in self._links.list_by_name(broadcast_artist_id)
            if link.state == LogNameLinkState.REJECTED
        }

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
