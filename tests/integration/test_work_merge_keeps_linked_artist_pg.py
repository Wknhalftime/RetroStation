"""A work merge keeps a local artist a log-name link points at (artist model phase 1a, SPEC
S11; AUD-R035, which changes AUD-R004).

When a merge takes a local artist's last work away, ``work_curation_service`` deletes the
artist only if nothing points at it. From 1a that means no work and no ``log_name_links`` row
in any state (active, suggested or rejected; a rejected pair must stay remembered, C19). An
unlinked local artist with no work left is still deleted, as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.work_curation_service import merge_works
from tests.matching_seed import seed_pg_link

type Conn = psycopg.Connection[DictRow]


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def _artist(conn: Conn, name: str) -> str:
    return RepositoryFactory(conn).artists.upsert_local_artist(name, name.lower())


def _work(conn: Conn, title: str, artist_id: str) -> str:
    return RepositoryFactory(conn).works.create_local(title, artist_id)


def _log_name(conn: Conn, name: str) -> BroadcastArtist:
    return RepositoryFactory(conn).broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name=name, normalized_name=name.lower())
    )


def _exists(conn: Conn, artist_id: str) -> bool:
    return conn.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,)).fetchone() is not None


@pytest.mark.parametrize(
    ("state", "origin"),
    [
        (LogNameLinkState.ACTIVE, LogNameLinkOrigin.AUTOMATIC),
        (LogNameLinkState.SUGGESTED, LogNameLinkOrigin.AUTOMATIC),
        (LogNameLinkState.REJECTED, LogNameLinkOrigin.MANUAL),
    ],
)
def test_linked_local_artist_survives(
    conn: Conn, state: LogNameLinkState, origin: LogNameLinkOrigin
) -> None:
    target = _work(conn, "Lithium", _artist(conn, "Nirvana"))
    linked = _artist(conn, "Kurt Solo")
    from_linked = _work(conn, "Lithium (solo)", linked)
    seed_pg_link(conn, _log_name(conn, "KURT SOLO").id, linked, state=state, origin=origin)
    unlinked = _artist(conn, "Krist Solo")
    from_unlinked = _work(conn, "Lithium (bass)", unlinked)
    conn.commit()

    merge_works(target, [from_linked, from_unlinked], work_curation_repos(RepositoryFactory(conn)))
    conn.commit()

    assert _exists(conn, linked)
    assert not _exists(conn, unlinked)
    links = conn.execute("SELECT state FROM log_name_links WHERE artist_id = %s", (linked,))
    assert [row["state"] for row in links.fetchall()] == [state.value]
