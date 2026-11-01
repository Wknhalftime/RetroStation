"""Seeds for the matching tests: a log name's link to an artist (artist model phase 1a, C73).

Locked, like ``tests/integration/stream_seed.py``: the Tier 1 test files seed their link
through it, so a later phase that changes how a link is seeded relocks this one file and Lance
sees the change.

Before phase 1a these tests seeded a ``matches`` artist row whose ``target_id`` was a catalog
artist's id or an MBID. A link is seeded the way 1a's migration copies such a row (S2, S3):
the target is a catalog artist's id, else an MBID a catalog artist holds, else an MBID that
gets a stub artist. It works on the fakes and on PostgreSQL alike, through the two ports.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.domain.catalog import Artist
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState
from backend.domain.matching import LogNameLink
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.log_name_links import LogNameLinkRepository


def link_target(catalog: ArtistCatalogRepository, target: str) -> Artist:
    """The catalog artist *target* names: the one with that id, else the one holding that
    MBID, else a new stub of that MBID."""
    return catalog.get_by_id(target) or catalog.get_by_mbid(target) or catalog.create_stub(target)


def seed_link(
    links: LogNameLinkRepository,
    catalog: ArtistCatalogRepository,
    broadcast_artist_id: UUID,
    target: str,
    *,
    origin: LogNameLinkOrigin = LogNameLinkOrigin.LEGACY,
    state: LogNameLinkState = LogNameLinkState.ACTIVE,
    strength: int = 100,
    created_at: datetime | None = None,
) -> LogNameLink:
    """Link the log name to the artist *target* names (``link_target``); the stored link."""
    artist = link_target(catalog, target)
    link = LogNameLink(
        broadcast_artist_id=broadcast_artist_id,
        artist_id=artist.id,
        mbid=artist.mbid,
        state=state,
        origin=origin,
        strength=strength,
        evidence={"version": 0, "seeded_target": target},
    )
    if created_at is not None:
        link = replace(link, created_at=created_at)
    return links.upsert_link(link)


def seed_pg_link(
    conn: psycopg.Connection[Any],
    broadcast_artist_id: UUID,
    target: str,
    *,
    origin: LogNameLinkOrigin = LogNameLinkOrigin.LEGACY,
    state: LogNameLinkState = LogNameLinkState.ACTIVE,
    strength: int = 100,
    created_at: datetime | None = None,
) -> LogNameLink:
    """``seed_link`` on PostgreSQL, through the adapters on *conn*."""
    return seed_link(
        PgLogNameLinkRepository(conn),
        PgArtistRepository(conn),
        broadcast_artist_id,
        target,
        origin=origin,
        state=state,
        strength=strength,
        created_at=created_at,
    )


def first_link(links: LogNameLinkRepository, broadcast_artist_id: UUID) -> LogNameLink | None:
    """The log name's oldest link, in any state, or None: what these tests read before 1a
    with ``MatchRepository.get_by_artist``."""
    found = links.list_by_name(broadcast_artist_id)
    return found[0] if found else None
