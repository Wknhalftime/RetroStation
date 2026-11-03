"""Migration 0045's rollback after the 1c matcher has run (artist model phase 1c, S2; the diff
review of PR 1).

The locked ``test_link_demotion_pg.py`` rolls back straight after the migration. In use, a
re-check rewinds every ``LINK_SUGGESTED`` name to pending and the matcher writes it again, in its
own words; and the matcher suggests for names 0045 never touched. The rollback must still put a
demoted name back, and leave no reason the code before 1c cannot read (its ``ReasonCode`` has
neither ``LINK_SUGGESTED`` nor ``LINK_REMOVED``, and reading one raises).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.domain.broadcast import (
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import ReasonCode
from backend.services.artist_matching_service import ArtistMatchingRepos, match_artists_for_playlist
from backend.services.normalization import compute_normalized_signature
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.mb_client import FakeMbClient
from tests.integration.demotion_seed import (
    Conn,
    before_the_demotion,
    catalog_artist,
    link,
    log_name,
    migrate,
    roll_back,
)

pytestmark = pytest.mark.integration

NEW_IN_1C = {ReasonCode.LINK_SUGGESTED.value, ReasonCode.LINK_REMOVED.value}


def _played(conn: Conn, name_ids: list[UUID]) -> UUID:
    """A playlist that played one song of each name; its id."""
    factory = RepositoryFactory(conn)
    station = factory.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KRBK"))
    playlist = factory.broadcast_playlists.create(
        BroadcastPlaylist(
            id=uuid4(), name="show.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )
    for name_id in name_ids:
        name = factory.broadcast_artists.get_by_id(name_id)
        assert name is not None
        song = factory.broadcast_identities.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=name_id,
                original_title="Song",
                normalized_title="song",
                normalized_signature=compute_normalized_signature(name.normalized_name, "song"),
            )
        )
        factory.broadcast_events.create(
            BroadcastPlayEvent(
                id=uuid4(),
                identity_id=song.id,
                playlist_id=playlist.id,
                played_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
    return playlist.id


def _match(conn: Conn, playlist_id: UUID) -> None:
    factory = RepositoryFactory(conn)
    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=ArtistMatchingRepos(
            broadcast_artist_repo=factory.broadcast_artists,
            track_identity_repo=factory.broadcast_identities,
            artist_repo=factory.artists,
            match_repo=factory.matches,
            rules_repo=FakeMappingRuleRepository(),
            link_repo=PgLogNameLinkRepository(conn),
        ),
        mb_client=FakeMbClient(),
    )


def _state(conn: Conn, name_id: UUID) -> tuple[str, str | None, str | None]:
    row = conn.execute(
        "SELECT match_status, reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
        (name_id,),
    ).fetchone()
    assert row is not None
    return row["match_status"], row["reason_code"], row["reason_detail"]


def _links(conn: Conn, name_id: UUID) -> dict[str, tuple[str, str]]:
    rows: list[dict[str, Any]] = conn.execute(
        "SELECT artist_id, state, origin FROM log_name_links WHERE broadcast_artist_id = %s",
        (name_id,),
    ).fetchall()
    return {row["artist_id"]: (row["state"], row["origin"]) for row in rows}


def test_rollback_after_a_recheck_and_a_run(migrated_db: str) -> None:
    with before_the_demotion(migrated_db) as conn:
        architects = catalog_artist(conn, "Architects", str(uuid4()))
        catalog_artist(conn, "Nirvana", str(uuid4()))
        catalog_artist(conn, "Nirvana", str(uuid4()))
        demoted = log_name(conn, "ARCHITECTS UK")  # 86.9 against Architects: a suggestion again
        link(conn, demoted, architects, score=66.0)
        rewound_only = log_name(conn, "ARCHITECTS USA")
        link(conn, rewound_only, architects, score=62.0)
        untouched = log_name(conn, "NIRVANA", "pending")  # 1c's matcher suggests both Nirvanas
        migrate(conn)
        assert _state(conn, demoted)[1] == "LINK_SUGGESTED"

        # A re-check rewinds the suggested names; the next run matches two of the three again.
        conn.execute(
            "UPDATE broadcast_artists SET match_status = 'pending', reason_code = NULL,"
            " reason_detail = NULL WHERE match_status = 'needs_review'"
        )
        _match(conn, _played(conn, [demoted, untouched]))
        rematched = _state(conn, demoted)
        suggested_fresh = _state(conn, untouched)

        roll_back(conn)

        after = {name_id: _state(conn, name_id) for name_id in (demoted, rewound_only, untouched)}
        demoted_links = _links(conn, demoted)
        left = conn.execute(
            "SELECT count(*) AS n FROM broadcast_artists WHERE reason_code = ANY(%s)",
            (list(NEW_IN_1C),),
        ).fetchone()

    assert rematched[:2] == ("needs_review", "LINK_SUGGESTED")
    assert rematched[2] is not None and "M22" not in rematched[2]  # the matcher's own words
    assert suggested_fresh[:2] == ("needs_review", "LINK_SUGGESTED")
    assert after[demoted] == ("auto_matched", None, None)
    assert demoted_links == {architects: ("active", "legacy")}
    assert after[rewound_only] == ("auto_matched", None, None)
    assert after[untouched] == ("pending", None, None)  # the old matcher decides it again
    assert left is not None and left["n"] == 0
