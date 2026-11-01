"""The links port's fake against its PostgreSQL adapter (artist model phase 1a, S5; C30).

The port and its fake are locked as complete (the SPEC's Context), so the same cases run on
``tests/fakes/log_name_links.py`` (built with the catalog and log-name fakes, for its foreign
keys) and on ``backend/db/repositories/log_name_links.py``, and the fake cannot drift from the
table: one row per (name, artist) in every state; an upsert of a held pair keeps its id and
``created_at`` and takes everything else; the lists run oldest first; a missing name or artist
raises ``LogNameLinkTargetMissingError`` and leaves the transaction usable; ``set_link_state``
says whether the pair has a row; ``delete_links_of_name`` says how many it removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import CreditRole, LogNameLinkOrigin, LogNameLinkState
from backend.domain.matching import (
    InvalidLogNameLinkError,
    LogNameLink,
    LogNameLinkTargetMissingError,
)
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Ports:
    links: LogNameLinkRepository
    artists: ArtistCatalogRepository
    names: BroadcastArtistRepository


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def ports(request: pytest.FixtureRequest) -> Iterator[Ports]:
    if request.param == "fake":
        artists, names = FakeArtistRepository(), FakeBroadcastArtistRepository()
        yield Ports(FakeLogNameLinkRepository(artists=artists, names=names), artists, names)
        return
    migrated_db: str = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        factory = RepositoryFactory(conn)
        yield Ports(PgLogNameLinkRepository(conn), factory.artists, factory.broadcast_artists)
        conn.rollback()


def _name(ports: Ports, spelling: str = "METALLICA") -> UUID:
    return ports.names.upsert(
        BroadcastArtist(id=uuid4(), original_name=spelling, normalized_name=spelling.lower())
    ).id


def _artist(ports: Ports, name: str = "Metallica") -> str:
    return ports.artists.upsert_local_artist(name, name.lower())


def _link(name_id: UUID, artist_id: str, **fields: Any) -> LogNameLink:
    values: dict[str, Any] = {
        "broadcast_artist_id": name_id,
        "artist_id": artist_id,
        "state": LogNameLinkState.ACTIVE,
        "origin": LogNameLinkOrigin.AUTOMATIC,
        "strength": 90,
        "evidence": {"version": 0, "tier": "normalization", "score": 90.0},
        "created_at": T0,
    }
    values.update(fields)
    return LogNameLink(**values)


def _key(link: LogNameLink) -> tuple[object, ...]:
    """Everything a stored link holds but its updated_at."""
    return (
        link.id,
        link.broadcast_artist_id,
        link.artist_id,
        link.mbid,
        link.state,
        link.origin,
        link.strength,
        dict(link.evidence),
        link.credit_part,
        link.role,
        link.join_after,
        link.created_at,
    )


def test_a_new_link_is_stored_as_given(ports: Ports) -> None:
    name_id, artist_id = _name(ports), _artist(ports)
    link = _link(
        name_id,
        artist_id,
        mbid="65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab",
        credit_part=1,
        role=CreditRole.FEATURED,
        join_after=" feat. ",
        evidence={"version": 0, "legacy": {"match_id": "m-1", "score": 1.0}},
    )

    stored = ports.links.upsert_link(link)

    assert _key(stored) == _key(link)
    assert [_key(found) for found in ports.links.list_by_name(name_id)] == [_key(link)]
    assert [_key(found) for found in ports.links.list_by_artist(artist_id)] == [_key(link)]


def test_one_row_per_pair_keeps_its_id_and_created_at(ports: Ports) -> None:
    name_id, artist_id = _name(ports), _artist(ports)
    first = ports.links.upsert_link(_link(name_id, artist_id))
    second = _link(
        name_id,
        artist_id,
        state=LogNameLinkState.REJECTED,
        origin=LogNameLinkOrigin.MANUAL,
        strength=100,
        evidence={"version": 0, "tier": "manual", "score": 1.0},
        mbid="5b11f4ce-a62d-471e-81fc-a69a8278c7da",
        credit_part=1,
        role=CreditRole.FEATURED,
        join_after=" feat. ",
        created_at=T0 + HOUR,
    )

    again = ports.links.upsert_link(second)

    [held] = ports.links.list_by_name(name_id)
    expected = _key(replace(second, id=first.id, created_at=T0))
    assert _key(held) == _key(again) == expected


def test_lists_run_oldest_first_and_keep_to_their_name_or_artist(ports: Ports) -> None:
    metallica, megadeth = _name(ports), _name(ports, "MEGADETH")
    a, b, c = (_artist(ports, n) for n in ("A Band", "B Band", "C Band"))
    ports.links.upsert_link(_link(metallica, a, created_at=T0 + 2 * HOUR))
    ports.links.upsert_link(_link(metallica, b, created_at=T0, state=LogNameLinkState.SUGGESTED))
    ports.links.upsert_link(_link(metallica, c, created_at=T0 + HOUR))
    ports.links.upsert_link(_link(megadeth, a, created_at=T0 - HOUR))

    by_name = [link.artist_id for link in ports.links.list_by_name(metallica)]
    by_artist = [link.broadcast_artist_id for link in ports.links.list_by_artist(a)]

    assert by_name == [b, c, a]
    assert by_artist == [megadeth, metallica]
    assert ports.links.list_by_name(uuid4()) == []
    assert ports.links.list_by_artist("no-such-artist") == []


def test_a_missing_name_or_artist_is_refused_and_the_rest_stands(ports: Ports) -> None:
    name_id, artist_id = _name(ports), _artist(ports)

    with pytest.raises(LogNameLinkTargetMissingError):
        ports.links.upsert_link(_link(name_id, "no-such-artist"))
    with pytest.raises(LogNameLinkTargetMissingError):
        ports.links.upsert_link(_link(uuid4(), artist_id))
    stored = ports.links.upsert_link(_link(name_id, artist_id))

    assert [link.id for link in ports.links.list_by_name(name_id)] == [stored.id]


def test_set_link_state_says_whether_the_pair_has_a_row(ports: Ports) -> None:
    name_id, artist_id = _name(ports), _artist(ports)
    ports.links.upsert_link(_link(name_id, artist_id))

    changed = ports.links.set_link_state(name_id, artist_id, LogNameLinkState.REJECTED)
    missing = ports.links.set_link_state(name_id, _artist(ports, "Other"), LogNameLinkState.ACTIVE)

    assert (changed, missing) == (True, False)
    [held] = ports.links.list_by_name(name_id)
    assert held.state == LogNameLinkState.REJECTED


def test_delete_links_of_name_removes_every_state_of_that_name_only(ports: Ports) -> None:
    name_id, other = _name(ports), _name(ports, "MEGADETH")
    states = (LogNameLinkState.ACTIVE, LogNameLinkState.SUGGESTED, LogNameLinkState.REJECTED)
    for state, artist in zip(states, ("A Band", "B Band", "C Band"), strict=True):
        ports.links.upsert_link(_link(name_id, _artist(ports, artist), state=state))
    kept = ports.links.upsert_link(_link(other, _artist(ports, "D Band")))

    removed = ports.links.delete_links_of_name(name_id)

    assert removed == 3
    assert ports.links.list_by_name(name_id) == []
    assert [link.id for link in ports.links.list_by_name(other)] == [kept.id]
    assert ports.links.delete_links_of_name(name_id) == 0


@pytest.mark.parametrize(
    "fields",
    [{"strength": -1}, {"strength": 101}, {"evidence": {"tier": "manual"}}, {"credit_part": -1}],
    ids=str,
)
def test_the_domain_refuses_what_the_table_would(fields: dict[str, Any]) -> None:
    with pytest.raises(InvalidLogNameLinkError):
        _link(uuid4(), "artist", **fields)
