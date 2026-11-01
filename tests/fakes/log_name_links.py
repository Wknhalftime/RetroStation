"""In-memory ``log_name_links`` (artist model phase 1a, S5; C30). Locked with the 1a tests.

The fake keeps the table's own rules and nothing else:

- one row per (log name, artist) in every state; an upsert of a held pair takes the new
  link's MBID, state, origin, strength, evidence and credit columns and keeps the row's id and
  ``created_at``;
- when built with the catalog (``artists``) and the log names (``names``), the two foreign
  keys: a link to an artist or a log name that does not exist raises
  ``LogNameLinkTargetMissingError``, as the PostgreSQL adapter does;
- the CHECKs (strength 0 to 100, evidence with a version, credit part not negative) are the
  domain's: ``LogNameLink`` refuses them when it is built.

The rules for links (a rejected pair stays rejected, placeholders refused, status from links)
live in ``LogNameLinkService``, never here, so later phases add rules without relocking this
file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from backend.domain.enums import LogNameLinkState
from backend.domain.matching import LogNameLink, LogNameLinkTargetMissingError
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.log_name_links import LogNameLinkRepository


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _oldest_first(links: Iterable[LogNameLink]) -> list[LogNameLink]:
    return sorted(links, key=lambda link: (link.created_at, str(link.id)))


class FakeLogNameLinkRepository(LogNameLinkRepository):
    def __init__(
        self,
        *,
        artists: ArtistCatalogRepository | None = None,
        names: BroadcastArtistRepository | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._rows: dict[tuple[UUID, str], LogNameLink] = {}
        self._artists = artists
        self._names = names
        self._clock = clock

    def list_by_name(self, broadcast_artist_id: UUID) -> list[LogNameLink]:
        return _oldest_first(
            link for (name_id, _), link in self._rows.items() if name_id == broadcast_artist_id
        )

    def list_by_artist(self, artist_id: str) -> list[LogNameLink]:
        return _oldest_first(link for link in self._rows.values() if link.artist_id == artist_id)

    def upsert_link(self, link: LogNameLink) -> LogNameLink:
        self._check_foreign_keys(link)
        key = (link.broadcast_artist_id, link.artist_id)
        held = self._rows.get(key)
        stored = replace(link, evidence=dict(link.evidence), updated_at=self._clock())
        if held is not None:
            stored = replace(stored, id=held.id, created_at=held.created_at)
        self._rows[key] = stored
        return stored

    def set_link_state(
        self, broadcast_artist_id: UUID, artist_id: str, state: LogNameLinkState
    ) -> bool:
        key = (broadcast_artist_id, artist_id)
        held = self._rows.get(key)
        if held is None:
            return False
        self._rows[key] = replace(held, state=state, updated_at=self._clock())
        return True

    def delete_links_of_name(self, broadcast_artist_id: UUID) -> int:
        doomed = [key for key in self._rows if key[0] == broadcast_artist_id]
        for key in doomed:
            del self._rows[key]
        return len(doomed)

    def _check_foreign_keys(self, link: LogNameLink) -> None:
        if self._artists is not None and self._artists.get_by_id(link.artist_id) is None:
            raise LogNameLinkTargetMissingError(f"no artist {link.artist_id}")
        if self._names is not None and self._names.get_by_id(link.broadcast_artist_id) is None:
            raise LogNameLinkTargetMissingError(f"no log name {link.broadcast_artist_id}")
