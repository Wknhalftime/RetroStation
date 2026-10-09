"""In-memory ArtistLinkingRepository for the local-artist linker's tests (AUD-R026, D15)."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.domain.catalog import (
    Artist,
    ArtistLinkCandidate,
    InvalidLinkDecisionError,
    LinkEvidence,
)
from backend.domain.enums import ArtistLinkOutcome, CatalogSource
from backend.repositories.artist_linking import ArtistLinkingRepository


class FakeArtistLinkingRepository(ArtistLinkingRepository):
    """Catalog artists, the tag evidence for each, when a file of each was last indexed, and
    the broadcast names matched to each.

    Mirrors the Pg adapter:
    - due: local, no MBID, and never looked up or a present file indexed after the lookup;
    - a link is refused when the artist already has an MBID or another artist holds this one;
    - a non-linked outcome is never written over an artist that has an MBID;
    - the names linked since a time: each linked artist's normalized name and its broadcast
      names.
    """

    def __init__(self) -> None:
        self.artists: dict[str, Artist] = {}
        self._evidence: dict[str, LinkEvidence] = {}
        self._file_indexed_at: dict[str, datetime] = {}
        self._broadcast_names: dict[str, set[str]] = {}
        self.evidence_calls: list[str] = []
        self.linked_names_asked: list[datetime] = []

    def add(
        self,
        artist: Artist,
        evidence: LinkEvidence | None = None,
        file_indexed_at: datetime | None = None,
        broadcast_names: tuple[str, ...] = (),
    ) -> Artist:
        self.artists[artist.id] = artist
        if evidence is not None and artist.normalized_name is not None:
            self._evidence[artist.normalized_name] = evidence
        if file_indexed_at is not None:
            self._file_indexed_at[artist.id] = file_indexed_at
        self._broadcast_names[artist.id] = set(broadcast_names)
        return artist

    def list_due(self) -> list[Artist]:
        due = [a for a in self.artists.values() if self._is_due(a)]
        return sorted(due, key=lambda a: (a.normalized_name or "", a.id))

    def _is_due(self, artist: Artist) -> bool:
        if artist.origin != CatalogSource.LOCAL or artist.mbid is not None:
            return False
        if artist.mb_lookup_at is None:
            return True
        indexed_at = self._file_indexed_at.get(artist.id)
        return indexed_at is not None and indexed_at > artist.mb_lookup_at

    def evidence(self, normalized_name: str) -> LinkEvidence:
        self.evidence_calls.append(normalized_name)
        return self._evidence.get(normalized_name, LinkEvidence())

    def link(self, artist_id: str, candidate: ArtistLinkCandidate) -> bool:
        artist = self.artists[artist_id]
        held = any(a.mbid == candidate.mbid for a in self.artists.values())
        if artist.mbid is not None or held:
            return False
        now = datetime.now(UTC)
        artist.mbid = candidate.mbid
        artist.origin = CatalogSource.MUSICBRAINZ
        artist.name = candidate.name
        artist.sort_name = candidate.sort_name
        artist.disambiguation = candidate.disambiguation
        artist.needs_enhancement = False
        artist.enhanced_at = now
        artist.mb_lookup_at = now
        artist.mb_lookup_outcome = ArtistLinkOutcome.LINKED
        return True

    def record_outcome(self, artist_id: str, outcome: ArtistLinkOutcome) -> None:
        if outcome == ArtistLinkOutcome.LINKED:
            raise InvalidLinkDecisionError("a link is written by link(), not record_outcome()")
        artist = self.artists[artist_id]
        if artist.mbid is not None:
            return
        artist.mb_lookup_at = datetime.now(UTC)
        artist.mb_lookup_outcome = outcome

    def normalized_names_linked_since(self, when: datetime) -> set[str]:
        self.linked_names_asked.append(when)
        names: set[str] = set()
        for artist in self.artists.values():
            if artist.mb_lookup_outcome != ArtistLinkOutcome.LINKED:
                continue
            if artist.mb_lookup_at is None or artist.mb_lookup_at <= when:
                continue
            if artist.normalized_name:
                names.add(artist.normalized_name)
            names |= self._broadcast_names.get(artist.id, set())
        return names
