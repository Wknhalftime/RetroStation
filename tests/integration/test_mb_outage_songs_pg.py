"""Acceptance tests on PostgreSQL: a MusicBrainz outage defers songs (D16, AUD-R028).

- rewind_mb_unavailable returns every needs_review / MB_UNAVAILABLE song, whatever its artist,
  to pending with tier and reason cleared. Other statuses (an approved song keeps its old
  reason), other reasons (DEFERRED_RETRY above all), artists and match rows are untouched.
- The artist cascade's reset (reset_deferred_by_artist_ids) still resets only DEFERRED_RETRY
  songs: an outage song under the same artist is not its business (Lance, 2026-10-12, Q1).
- End to end, the 2026-10-10 shape: Step B's recording search answers 503. The song is
  deferred and loses its old suggestion, the rest of the playlist is matched in the same run,
  and the next targeted re-check, whose wave does not name the song's artist, rewinds it and
  queues its playlist, leaving an artist-cascade deferral alone. With MusicBrainz back, the
  next run matches it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

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
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services import matching_reasons as reasons
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.matching_recheck_service import playlists_with_pending_work, rewind_wave
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from backend.services.title_scoring import broadcast_title_variants
from tests.fakes.mb_client import FakeMbClient
from tests.matching_seed import seed_pg_link
from tests.services.test_mb_outage_songs import _http_error, _Mb

Conn = psycopg.Connection[Any]


def _connect(url: str) -> Conn:
    return psycopg.connect(url, row_factory=dict_row)


# --- Seed helpers --------------------------------------------------------------------------


def _artist(
    conn: Conn,
    name: str,
    status: MatchStatus = MatchStatus.AUTO_MATCHED,
    reason: ReasonCode | None = None,
    *,
    mbid: str | None = None,
) -> BroadcastArtist:
    """A broadcast artist; with ``mbid``, a link to the artist of that MBID (Tier 1)."""
    repo = PgBroadcastArtistRepository(conn)
    artist = repo.upsert(
        BroadcastArtist(
            id=uuid4(),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=status,
        )
    )
    repo.update_match_status(artist.id, status, reason, None if reason is None else "stale")
    if mbid is not None:
        seed_pg_link(conn, artist.id, mbid)
    return artist


def _song(
    conn: Conn,
    artist: BroadcastArtist,
    title: str,
    status: MatchStatus = MatchStatus.PENDING,
    reason: ReasonCode | None = None,
) -> BroadcastTrackIdentity:
    repo = PgBroadcastTrackIdentityRepository(conn)
    norm = normalize_title(title)
    song = repo.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=norm,
            normalized_signature=compute_normalized_signature(artist.normalized_name, norm),
        )
    )
    if status != MatchStatus.PENDING:
        repo.update_match_status(song.id, status, MatchTier.UNCLASSIFIED, reason, "stale")
    return song


def _file(
    conn: Conn,
    artist: str,
    title: str,
    *,
    artist_mbid: str | None = None,
    recording_mbid: str | None = None,
) -> LibraryFile:
    return PgLibraryFileRepository(conn).upsert(
        LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                track_title=title,
                normalized_title=normalize_title(title),
                artist_name=artist,
                normalized_artist_name=normalize_artist(artist),
                artist_mbid=artist_mbid,
                recording_mbid=recording_mbid,
            ),
        )
    )


def _suggest(conn: Conn, song: BroadcastTrackIdentity, file_id: UUID) -> Match:
    return PgMatchRepository(conn).create(
        Match(
            id=uuid4(),
            identity_id=song.id,
            library_file_id=file_id,
            confidence_score=60.0,
            match_tier=MatchTier.LOCAL_FILE_FUZZY,
        )
    )


def _playlist(conn: Conn) -> BroadcastPlaylist:
    station = PgBroadcastStationRepository(conn).create(
        BroadcastStation(id=uuid4(), call_letters="KMBZ")
    )
    return PgBroadcastPlaylistRepository(conn).create(
        BroadcastPlaylist(
            id=uuid4(), name=f"{uuid4().hex}.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )


def _play(conn: Conn, playlist: BroadcastPlaylist, song: BroadcastTrackIdentity) -> None:
    PgBroadcastPlayEventRepository(conn).create(
        BroadcastPlayEvent(
            id=uuid4(), identity_id=song.id, playlist_id=playlist.id, played_at=datetime.now(UTC)
        )
    )


def _state(conn: Conn, song: BroadcastTrackIdentity) -> tuple[Any, ...]:
    row = conn.execute(
        """SELECT match_status, match_tier, reason_code, reason_detail
             FROM track_identities WHERE id = %s""",
        (song.id,),
    ).fetchone()
    assert row is not None
    return tuple(row.values())


def _rows(conn: Conn, song: BroadcastTrackIdentity) -> list[UUID]:
    found = conn.execute(
        "SELECT library_file_id FROM matches WHERE identity_id = %s ORDER BY id", (song.id,)
    ).fetchall()
    return [r["library_file_id"] for r in found]


def _match(conn: Conn, playlist: BroadcastPlaylist, mb: FakeMbClient) -> None:
    match_identities_for_playlist(
        playlist_id=playlist.id,
        repos=IdentityMatchingRepos(
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=PgLibraryFileRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
            catalog_repo=PgArtistRepository(conn),
            link_repo=PgLogNameLinkRepository(conn),
        ),
        mb_client=mb,
    )


PENDING = (MatchStatus.PENDING.value, None, None, None)


# --- rewind_mb_unavailable -----------------------------------------------------------------


def test_rewind_mb_unavailable_returns_every_outage_song_to_pending(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        outage_reason = ReasonCode.MB_UNAVAILABLE
        deferred = ReasonCode.DEFERRED_RETRY
        queen = _artist(conn, "Queen", mbid="mbid-queen")
        blondie = _artist(conn, "Blondie", MatchStatus.NEEDS_REVIEW, deferred)
        outage = _song(conn, queen, "Bohemian Rhapsody", MatchStatus.NEEDS_REVIEW, outage_reason)
        # Whatever its artist: an outage song under an undecided artist is rewound too.
        elsewhere = _song(conn, blondie, "Heart of Glass", MatchStatus.NEEDS_REVIEW, outage_reason)
        cascaded = _song(conn, blondie, "Call Me", MatchStatus.NEEDS_REVIEW, deferred)
        review = _song(
            conn, queen, "Radio Ga Ga", MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE
        )
        # Approve and the artist reject cascade change the status only, keeping the reason.
        approved = _song(conn, queen, "Under Pressure", MatchStatus.MANUAL_MATCHED, outage_reason)
        rejected = _song(conn, blondie, "Atomic", MatchStatus.AUTO_REJECTED, outage_reason)
        kept = _suggest(conn, approved, _file(conn, "Queen", "Under Pressure").id)

        changed = PgBroadcastTrackIdentityRepository(conn).rewind_mb_unavailable()

        assert changed == 2
        assert _state(conn, outage) == PENDING
        assert _state(conn, elsewhere) == PENDING
        assert _state(conn, cascaded)[::2] == (MatchStatus.NEEDS_REVIEW.value, deferred.value)
        assert _state(conn, review)[0] == MatchStatus.NEEDS_REVIEW.value
        assert _state(conn, approved)[0] == MatchStatus.MANUAL_MATCHED.value
        assert _state(conn, rejected)[0] == MatchStatus.AUTO_REJECTED.value
        assert _rows(conn, approved) == [kept.library_file_id]
        artist = PgBroadcastArtistRepository(conn).get_by_id(blondie.id)
        assert artist is not None
        assert (artist.match_status, artist.reason_code) == (MatchStatus.NEEDS_REVIEW, deferred)


def test_the_artist_cascade_reset_leaves_outage_songs_alone(migrated_db: str) -> None:
    """Artist matching's per-playlist reset keeps its own DEFERRED_RETRY scope (Q1)."""
    with _connect(migrated_db) as conn:
        repo = PgBroadcastTrackIdentityRepository(conn)
        deferred = ReasonCode.DEFERRED_RETRY
        abba = _artist(conn, "ABBA", MatchStatus.NEEDS_REVIEW, deferred)
        cascaded = _song(conn, abba, "Waterloo", MatchStatus.NEEDS_REVIEW, deferred)
        outage = _song(conn, abba, "SOS", MatchStatus.NEEDS_REVIEW, ReasonCode.MB_UNAVAILABLE)

        assert repo.reset_deferred_by_artist_ids([abba.id]) == 1

        assert _state(conn, cascaded) == PENDING
        assert _state(conn, outage)[2] == ReasonCode.MB_UNAVAILABLE.value


# --- End to end ----------------------------------------------------------------------------


def test_an_outage_defers_a_song_on_postgres_and_the_next_recheck_heals_it(
    migrated_db: str,
) -> None:
    with _connect(migrated_db) as conn:
        playlist = _playlist(conn)
        marvelettes = _artist(conn, "The Marvelettes", mbid="mbid-marvelettes")
        beatles = _artist(conn, "The Beatles", mbid="mbid-beatles")
        deferred_artist = _artist(
            conn, "Blondie", MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY
        )
        cascaded = _song(
            conn, deferred_artist, "Call Me", MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY
        )
        postman = _song(conn, marvelettes, "Please Mr Postman")
        live = _file(conn, "The Marvelettes", "Please Mr Postman (Live)")
        _suggest(conn, postman, live.id)  # the old suggestion a rewind kept
        # Not tagged with the artist MBID, so Step A is empty and Step B is needed. Step C
        # would auto-match it (exact title): it must not decide the song while Step B fails.
        found = _file(conn, "The Marvelettes", "Please Mr Postman", recording_mbid="rec-postman")
        help_song = _song(conn, beatles, "Help")
        tagged = _file(conn, "The Beatles", "Help", artist_mbid="mbid-beatles")
        for song in (postman, help_song):
            _play(conn, playlist, song)

        _match(conn, playlist, _Mb(down=_http_error(503)))

        assert _state(conn, postman) == (
            MatchStatus.NEEDS_REVIEW.value,
            MatchTier.UNCLASSIFIED.value,
            ReasonCode.MB_UNAVAILABLE.value,
            reasons.format_mb_unavailable(),
        )
        assert _rows(conn, postman) == []
        assert _state(conn, help_song)[0] == MatchStatus.AUTO_MATCHED.value
        assert _rows(conn, help_song) == [tagged.id]

        # A targeted re-check whose wave names none of the artists.
        artists = PgBroadcastArtistRepository(conn)
        songs = PgBroadcastTrackIdentityRepository(conn)
        counts = rewind_wave({normalize_artist("Somebody Else")}, artists, songs)

        assert counts.songs == 1
        assert _state(conn, postman) == PENDING
        assert _state(conn, cascaded)[2] == ReasonCode.DEFERRED_RETRY.value  # its own path
        assert playlists_with_pending_work(artists, songs) == {playlist.id}

        searched = broadcast_title_variants("Please Mr Postman")[0]
        _match(
            conn,
            playlist,
            _Mb(recording_searches={("mbid-marvelettes", searched): [{"id": "rec-postman"}]}),
        )

        assert _state(conn, postman)[:2] == (
            MatchStatus.AUTO_MATCHED.value,
            MatchTier.MUSICBRAINZ_ID_SEARCH.value,
        )
        assert _rows(conn, postman) == [found.id]
