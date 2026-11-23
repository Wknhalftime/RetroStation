"""An artist link made while a matching run is going, against PostgreSQL with two connections.

The unit-level race is tests/services/test_stale_artist_link_read.py. The match job writes a
whole playlist in one transaction and commits at the end (``tasks/identity_matching_tasks.py``),
and the link's cascade rewrites every pending song of the artist (``_LINK_REWIND_STATUSES``
includes pending). So:

- if the run has already written one of the artist's songs, the cascade waits on that row lock
  until the run commits, then re-checks the rows and rewinds them; the link's job re-matches
  them. Correct, but the API's link request waits for the whole run;
- if the run has not yet written any of the artist's songs, the cascade commits at once, and the
  run then scores those songs from the artist it read at stage start. Their guarded writes see
  pending and land, and the job the link enqueued, which reads only pending songs, skips them.

This test is the second case: a playlist with another artist's song first, and the curator
links Hall & Oates at the first tick where none of their songs is written yet (checked on the
worker's own connection). The link's writes are the MANUAL_MATCHED artist resolve's statements
(``services/artist_resolution_service.py`` ``match_by_hand``), on their own autocommit
connection. The scoring
setup is the unit test's: near titles that tie on the name path, so the pre-link read sends a
song to review and the link's Step A auto-matches it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.artist_resolution import _LINK_REWIND_STATUSES
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import EnrichmentStatus, LogNameLinkOrigin, MatchStatus, MatchTier
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.mb_client import FakeMbClient
from tests.matching_seed import seed_pg_link

Conn = psycopg.Connection[Any]

ARTIST = "Hall & Oates"
NORM = normalize_artist(ARTIST)
ARTIST_MBID = "a0b5a6c4-0000-4000-8000-000000000001"
SONGS = {
    "Rich Girl": ("Rick Girl", "Rich Gurl"),  # 88.9 each: tagged, untagged
    "Sara Smile": ("Sara Smyle", "Sara Smilr"),  # 90.0 each
}
OTHER_TAGGED = "Maneater"
FIRST_ARTIST = "Todd Rundgren"  # its song is inserted first, so the run reaches it first
FIRST_TITLE = "Hello It's Me"


def _connect(url: str, *, autocommit: bool = False) -> Conn:
    return psycopg.connect(url, row_factory=dict_row, autocommit=autocommit)


def _file(conn: Conn, title: str, artist_mbid: str | None) -> LibraryFile:
    return PgLibraryFileRepository(conn).upsert(
        LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                track_title=title,
                normalized_title=normalize_title(title),
                artist_name=ARTIST,
                normalized_artist_name=NORM,
                artist_mbid=artist_mbid,
            ),
        )
    )


def _song(conn: Conn, artist: BroadcastArtist, title: str, playlist_id: UUID) -> UUID:
    song = PgBroadcastTrackIdentityRepository(conn).upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=normalize_title(title),
            normalized_signature=compute_normalized_signature(
                artist.normalized_name, normalize_title(title)
            ),
        )
    )
    PgBroadcastPlayEventRepository(conn).create(
        BroadcastPlayEvent(
            id=uuid4(), identity_id=song.id, playlist_id=playlist_id, played_at=datetime.now(UTC)
        )
    )
    return song.id


class _Seed:
    """One playlist: another artist's song first, then two Hall & Oates songs, all unresolved;
    committed before the run starts."""

    def __init__(self, url: str) -> None:
        with _connect(url) as conn:
            first = PgBroadcastArtistRepository(conn).upsert(
                BroadcastArtist(
                    id=uuid4(),
                    original_name=FIRST_ARTIST,
                    normalized_name=normalize_artist(FIRST_ARTIST),
                )
            )
            self.artist = PgBroadcastArtistRepository(conn).upsert(
                BroadcastArtist(id=uuid4(), original_name=ARTIST, normalized_name=NORM)
            )
            station = PgBroadcastStationRepository(conn).create(
                BroadcastStation(id=uuid4(), call_letters="WHAO")
            )
            self.playlist = PgBroadcastPlaylistRepository(conn).create(
                BroadcastPlaylist(
                    id=uuid4(),
                    name=f"{uuid4().hex}.csv",
                    content_hash=uuid4().hex,
                    station_id=station.id,
                )
            )
            _song(conn, first, FIRST_TITLE, self.playlist.id)
            self.tagged_file: dict[UUID, UUID] = {}
            for title, (tagged, untagged) in SONGS.items():
                song_id = _song(conn, self.artist, title, self.playlist.id)
                self.tagged_file[song_id] = _file(conn, tagged, ARTIST_MBID).id
                _file(conn, untagged, None)
            _file(conn, OTHER_TAGGED, ARTIST_MBID)
            conn.commit()


def _link_artist(conn: Conn, artist_id: UUID) -> None:
    """match_by_hand's MANUAL_MATCHED writes, in one transaction: the name's status, its
    manual link to the artist holding the MBID (a stub here), the rewind and the delete."""
    conn.execute("SET lock_timeout = '5s'")
    with conn.transaction():
        conn.execute(
            "UPDATE broadcast_artists SET match_status = %s WHERE id = %s",
            (MatchStatus.MANUAL_MATCHED.value, artist_id),
        )
        seed_pg_link(conn, artist_id, ARTIST_MBID, origin=LogNameLinkOrigin.MANUAL)
        conn.execute(
            """UPDATE track_identities
                  SET match_status = %s, match_tier = NULL,
                      reason_code = NULL, reason_detail = NULL
                WHERE broadcast_artist_id = %s AND match_status = ANY(%s)""",
            (MatchStatus.PENDING.value, artist_id, _LINK_REWIND_STATUSES),
        )
        conn.execute(
            """DELETE FROM matches
                WHERE identity_id IN (
                    SELECT id FROM track_identities
                     WHERE broadcast_artist_id = %s AND match_status = %s)""",
            (artist_id, MatchStatus.PENDING.value),
        )


class _CuratorLinksArtist:
    """At the first tick where the run has written none of Hall & Oates' songs (as its own
    connection sees them), the curator links the artist on another connection."""

    def __init__(self, url: str, seed: _Seed) -> None:
        self._url = url
        self._seed = seed
        self.worker: Conn | None = None
        self.linked = False

    def tick(self) -> None:
        if self.linked or self.worker is None:
            return
        rows = self.worker.execute(
            "SELECT match_status FROM track_identities WHERE id = ANY(%s)",
            (list(self._seed.tagged_file),),
        ).fetchall()
        if any(row["match_status"] != MatchStatus.PENDING.value for row in rows):
            return
        with _connect(self._url, autocommit=True) as api:
            _link_artist(api, self._seed.artist.id)
        self.linked = True


def _run(url: str, seed: _Seed, progress: _CuratorLinksArtist | None = None) -> None:
    """One identity-matching job: one connection, one commit at the end, as the task does."""
    with _connect(url) as conn:
        repos = IdentityMatchingRepos(
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=PgLibraryFileRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
            catalog_repo=PgArtistRepository(conn),
            link_repo=PgLogNameLinkRepository(conn),
        )
        if progress is None:
            match_identities_for_playlist(
                playlist_id=seed.playlist.id, repos=repos, mb_client=FakeMbClient()
            )
        else:
            progress.worker = conn
            match_identities_for_playlist(
                playlist_id=seed.playlist.id,
                repos=repos,
                mb_client=FakeMbClient(),
                progress=progress,
            )
        conn.commit()


def _outcome(url: str, song_id: UUID) -> tuple[str, UUID | None]:
    with _connect(url) as conn:
        row = conn.execute(
            """SELECT ti.match_status, m.library_file_id
                 FROM track_identities ti
                 LEFT JOIN matches m ON m.identity_id = ti.id
                WHERE ti.id = %s""",
            (song_id,),
        ).fetchone()
    assert row is not None
    return str(row["match_status"]), row["library_file_id"]


def test_songs_read_before_an_artist_link_are_matched_as_the_link_says(
    migrated_db: str,
) -> None:
    seed = _Seed(migrated_db)
    curator = _CuratorLinksArtist(migrated_db, seed)

    _run(migrated_db, seed, progress=curator)  # the run the link interrupts
    _run(migrated_db, seed)  # the job the link enqueued

    # The setup did what the docstring says: the curator linked the artist mid-run, before
    # the run had written any of its songs.
    assert curator.linked
    outcomes = {song_id: _outcome(migrated_db, song_id) for song_id in seed.tagged_file}
    expected = {
        song_id: (MatchStatus.AUTO_MATCHED.value, file_id)
        for song_id, file_id in seed.tagged_file.items()
    }
    assert outcomes == expected, (
        f"after the link and its job, every song should auto-match its tagged file; got {outcomes}"
    )


def test_the_guarded_write_lands_while_the_artist_has_the_status_read(migrated_db: str) -> None:
    seed = _Seed(migrated_db)
    song_id = next(iter(seed.tagged_file))
    with _connect(migrated_db) as conn:
        wrote = PgBroadcastTrackIdentityRepository(conn).update_match_status_if_pending(
            song_id,
            MatchStatus.NEEDS_REVIEW,
            MatchTier.LOCAL_FILE_FUZZY,
            artist_status_read=MatchStatus.PENDING,
        )
        conn.commit()

    assert wrote
    assert _outcome(migrated_db, song_id)[0] == MatchStatus.NEEDS_REVIEW.value


def test_the_guarded_write_skips_when_the_artist_changed_since_the_read(
    migrated_db: str,
) -> None:
    seed = _Seed(migrated_db)
    song_id = next(iter(seed.tagged_file))
    with _connect(migrated_db, autocommit=True) as api:
        _link_artist(api, seed.artist.id)
    with _connect(migrated_db) as conn:
        wrote = PgBroadcastTrackIdentityRepository(conn).update_match_status_if_pending(
            song_id,
            MatchStatus.NEEDS_REVIEW,
            MatchTier.LOCAL_FILE_FUZZY,
            artist_status_read=MatchStatus.PENDING,
        )
        conn.commit()

    assert not wrote
    assert _outcome(migrated_db, song_id)[0] == MatchStatus.PENDING.value


def test_control_linked_before_the_run_every_song_auto_matches_its_tagged_file(
    migrated_db: str,
) -> None:
    seed = _Seed(migrated_db)
    with _connect(migrated_db, autocommit=True) as api:
        _link_artist(api, seed.artist.id)

    _run(migrated_db, seed)

    for song_id, file_id in seed.tagged_file.items():
        assert _outcome(migrated_db, song_id) == (MatchStatus.AUTO_MATCHED.value, file_id)
