"""Every log name's status agrees with its links, on PostgreSQL (artist model phase 1c, SPEC S3;
C36, AUD-R038).

The world starts as 0044 and 1a's and 1b's writers left it: fuzzy, exact, manual, placeholder
and rejected-name links, a listed name, and names in every status. Migration 0045 runs, then
every writer once, through the PostgreSQL adapters and the services the task and the routes
call: the artist matcher over a playlist of pending names (an exact name, an exact name with
two artists, a lead-rule match, a rejected pair, a name with nothing, a rewound suggested
name), Resolve accepting a suggestion, Reject and Unmatch.

Afterwards every row of ``broadcast_artists`` agrees with its links:

- a pending name holds no active link (the re-check's rewind may leave it suggestions: the
  rewind is a writer, and the next run decides it);
- every other name's status and reason are what ``status_from_links`` gives for its links when
  no writer proposes anything: the rule's fixed point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import LogNameLinkState, MatchStatus, ReasonCode
from backend.domain.log_name_status import NameState, status_from_links
from backend.services.artist_matching_service import ArtistMatchingRepos, match_artists_for_playlist
from backend.services.artist_resolution_service import match_by_hand, reject_by_hand, unmatch
from backend.services.normalization import compute_normalized_signature, normalize_artist
from backend.services.repository_factory import RepositoryFactory, artist_resolution_repos
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.mb_client import FakeMbClient
from tests.integration.demotion_seed import (
    Conn,
    before_the_demotion,
    catalog_artist,
    link,
    log_name,
    migrate,
)

pytestmark = pytest.mark.integration

VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"


def _old_world(conn: Conn) -> dict[str, UUID]:
    """Names and links as they were before 0045; the names Resolve, Reject and Unmatch act on."""
    architects = catalog_artist(conn, "Architects", str(uuid4()))
    archies = catalog_artist(conn, "Archies", str(uuid4()))
    beth_hart = catalog_artist(conn, "Beth Hart", str(uuid4()))
    beth_harte = catalog_artist(conn, "Beth Harte", str(uuid4()))
    various = catalog_artist(conn, "Various Artists", VARIOUS_ARTISTS)
    local = catalog_artist(conn, "Van Halen")

    link(conn, log_name(conn, "ARCHIES"), archies, score=72.4)  # demoted
    link(conn, log_name(conn, "ARCHIE"), archies, score=50.0)  # removed
    manual = log_name(conn, "BETH HART", "manual_matched")  # E1
    link(conn, manual, beth_hart, origin="manual")
    link(conn, manual, beth_harte, score=58.0)
    link(conn, log_name(conn, "VARIOUS HITS"), various, tier="musicbrainz_api")
    link(conn, log_name(conn, "REJECTED ACT", "manual_rejected"), architects)
    link(conn, log_name(conn, "VAN HALEN"), local)
    link(conn, log_name(conn, "MAPPED", "auto_matched"), architects, tier="manual")
    spot = log_name(conn, "SPOT BREAK", "needs_review", "DEFERRED_RETRY", "deferred")
    link(conn, spot, architects, score=60.0)
    log_name(conn, "OLD REVIEW", "needs_review", "LOW_CONFIDENCE", "Score 52%")
    log_name(conn, "NEVER PLAYED", "pending")
    rewound = log_name(conn, "REWOUND UNPLAYED", "pending")
    link(conn, rewound, archies, state="suggested", score=61.0)

    to_accept = log_name(conn, "ARCHITECTS UK")
    link(conn, to_accept, architects, score=66.0)
    to_reject = log_name(conn, "ARCHITECTS")
    link(conn, to_reject, architects)
    to_unmatch = log_name(conn, "BETH HARTE", "manual_matched")
    link(conn, to_unmatch, beth_harte, origin="manual")
    return {"accept": to_accept, "reject": to_reject, "unmatch": to_unmatch}


def _played(conn: Conn) -> tuple[UUID, dict[str, UUID]]:
    """A playlist of pending names, each with one song, and the catalog they meet; the
    playlist and the names."""
    factory = RepositoryFactory(conn)
    station = factory.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KRSC"))
    playlist = factory.broadcast_playlists.create(
        BroadcastPlaylist(
            id=uuid4(), name="show.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )
    for name, mbid in (
        ("Metallica", str(uuid4())),
        ("Nirvana", str(uuid4())),
        ("Nirvana", str(uuid4())),
        ("Artifas", str(uuid4())),
        ("The Audacity", str(uuid4())),
        ("B.O.C.", str(uuid4())),
    ):
        catalog_artist(conn, name, mbid)

    names: dict[str, UUID] = {}
    for spelling in (
        "METALLICA",
        "NIRVANA",
        "ANTI-FLAG",
        "AQUEDUCT",
        "ZZZYX QWERTY",
        "BLUE OYSTER CULT",
    ):
        name = factory.broadcast_artists.upsert(
            BroadcastArtist(
                id=uuid4(), original_name=spelling, normalized_name=normalize_artist(spelling)
            )
        )
        song = factory.broadcast_identities.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=name.id,
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
        names[spelling] = name.id

    def artist_named(name: str) -> str:
        row = conn.execute("SELECT id FROM artists WHERE name = %s", (name,)).fetchone()
        assert row is not None
        return str(row["id"])

    # The curator rejected The Audacity for AQUEDUCT; BLUE OYSTER CULT was rewound with a
    # suggestion the matcher no longer finds.
    link(conn, names["AQUEDUCT"], artist_named("The Audacity"), state="rejected", origin="manual")
    link(conn, names["BLUE OYSTER CULT"], artist_named("B.O.C."), state="suggested", score=58.0)
    return playlist.id, names


def _disagreements(conn: Conn) -> tuple[list[str], set[tuple[str, str | None]]]:
    """Every row whose status disagrees with its links, and the (status, reason) pairs seen."""
    links = PgLogNameLinkRepository(conn)
    wrong: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    rows: list[dict[str, Any]] = conn.execute(
        "SELECT id, original_name, match_status, reason_code, reason_detail"
        " FROM broadcast_artists ORDER BY original_name"
    ).fetchall()
    for row in rows:
        current = NameState(
            MatchStatus(row["match_status"]),
            ReasonCode(row["reason_code"]) if row["reason_code"] else None,
            row["reason_detail"],
        )
        seen.add((current.status.value, row["reason_code"]))
        held = links.list_by_name(row["id"])
        if current.status == MatchStatus.PENDING:
            if any(link.state == LogNameLinkState.ACTIVE for link in held):
                wrong.append(f"{row['original_name']}: pending with an active link")
            continue
        derived = status_from_links(current, held)
        if (derived.status, derived.reason_code) != (current.status, current.reason_code):
            wrong.append(
                f"{row['original_name']}: {current.status.value}/{current.reason_code}"
                f" but its links say {derived.status.value}/{derived.reason_code}"
            )
    return wrong, seen


def test_every_row_agrees(migrated_db: str) -> None:
    with before_the_demotion(migrated_db) as conn:
        acted_on = _old_world(conn)
        migrate(conn)
        after_migration, _ = _disagreements(conn)

        playlist_id, _ = _played(conn)
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
        resolution = artist_resolution_repos(factory)
        suggested = conn.execute(
            "SELECT artist_id FROM log_name_links WHERE broadcast_artist_id = %s",
            (acted_on["accept"],),
        ).fetchone()
        assert suggested is not None
        match_by_hand(acted_on["accept"], suggested["artist_id"], resolution)
        reject_by_hand(acted_on["reject"], resolution)
        unmatch(acted_on["unmatch"], resolution)

        after_writers, seen = _disagreements(conn)

    assert after_migration == []
    assert after_writers == []
    # The world reaches every kind of row the rule distinguishes.
    assert {
        ("pending", None),
        ("auto_matched", None),
        ("manual_matched", None),
        ("manual_rejected", None),
        ("needs_review", "LINK_SUGGESTED"),
        ("needs_review", "LINK_REMOVED"),
        ("needs_review", "LINK_REJECTED"),
        ("needs_review", "DEFERRED_RETRY"),
        ("needs_review", "USER_UNMATCHED"),
        ("needs_review", "LOW_CONFIDENCE"),
    } <= seen
