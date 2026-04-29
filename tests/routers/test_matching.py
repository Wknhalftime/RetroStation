from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import EnrichmentStatus, MatchStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.routers.matching import (
    QueueIdentity,
    _artist_bucket_from_identities,
    _compute_triage_bucket,
)
from backend.services.matching_constants import MIN_PRESENTATION_SCORE

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _insert_station(conn: psycopg.Connection, call_letters: str = "KAZR-FM") -> BroadcastStation:
    station = BroadcastStation(id=uuid4(), call_letters=call_letters)
    result = PgBroadcastStationRepository(conn).create(station)
    conn.commit()
    return result


def _insert_playlist(
    conn: psycopg.Connection,
    station: BroadcastStation,
    name: str = "show.csv",
) -> BroadcastPlaylist:
    playlist = BroadcastPlaylist(
        id=uuid4(),
        name=name,
        content_hash=uuid4().hex,
        station_id=station.id,
    )
    result = PgBroadcastPlaylistRepository(conn).create(playlist)
    conn.commit()
    return result


def _insert_artist(
    conn: psycopg.Connection,
    original_name: str = "Test Artist",
    match_status: MatchStatus = MatchStatus.NEEDS_REVIEW,
    with_candidates: bool = False,
) -> BroadcastArtist:
    artist = BroadcastArtist(
        id=uuid4(),
        original_name=original_name,
        normalized_name=original_name.lower(),
        match_status=match_status,
        artist_candidates=(
            [{"mbid": "abc-123", "name": original_name, "score": 100}]
            if with_candidates
            else None
        ),
    )
    # upsert won't persist candidates; insert directly so candidates land in JSONB column
    conn.execute(
        """
        INSERT INTO broadcast_artists (
            id, original_name, normalized_name, match_status, artist_candidates
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            artist.id,
            artist.original_name,
            artist.normalized_name,
            artist.match_status.value,
            json.dumps(artist.artist_candidates) if artist.artist_candidates else None,
        ),
    )
    conn.commit()
    return artist


def _insert_identity(
    conn: psycopg.Connection,
    artist: BroadcastArtist,
    original_title: str = "Test Song",
    match_status: MatchStatus = MatchStatus.PENDING,
) -> BroadcastTrackIdentity:
    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title=original_title,
        normalized_title=original_title.lower(),
        normalized_signature=f"{artist.normalized_name}:{original_title.lower()}:{uuid4().hex}",
        match_status=match_status,
    )
    result = PgBroadcastTrackIdentityRepository(conn).upsert(identity)
    conn.commit()
    return result


def _insert_event(
    conn: psycopg.Connection,
    playlist: BroadcastPlaylist,
    identity: BroadcastTrackIdentity,
) -> BroadcastPlayEvent:
    event = BroadcastPlayEvent(
        id=uuid4(),
        identity_id=identity.id,
        playlist_id=playlist.id,
        played_at=datetime.now(tz=UTC),
    )
    result = PgBroadcastPlayEventRepository(conn).create(event)
    conn.commit()
    return result


def _insert_library_file(
    conn: psycopg.Connection,
    *,
    track_title: str | None = None,
    release_title: str | None = None,
    recording_mbid: str | None = None,
    recording_id: str | None = None,
    work_id: str | None = None,
    artist_name: str | None = "Review Artist",
    normalized_artist_name: str | None = "review artist",
) -> LibraryFile:
    """The /queue endpoint joins library_files.normalized_artist_name against
    the locked broadcast_artists.normalized_name as the locked-artist guard.
    Defaults match `_seed_review_chain`'s artist_name="Review Artist" so the
    common case "I just want any library file" continues to satisfy the JOIN.
    Pass an explicit normalized_artist_name when seeding a wrong-artist file
    or pairing the file with a non-default broadcast artist.
    """
    lf = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        file_hash=uuid4().hex,
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        recording_id=recording_id,
        work_id=work_id,
        audio=AudioMetadata(
            track_title=track_title,
            release_title=release_title,
            recording_mbid=recording_mbid,
            artist_name=artist_name,
            normalized_artist_name=normalized_artist_name,
        ),
    )
    result = PgLibraryFileRepository(conn).upsert(lf)
    conn.commit()
    return result


def _insert_canonical_artist(conn: psycopg.Connection, artist_id: str) -> str:
    """Minimal canonical artist row for satisfying the works.artist_id FK
    when tests need a Work to test work_id propagation.
    """
    conn.execute(
        "INSERT INTO artists (id, name, sort_name) VALUES (%s, %s, %s)",
        (artist_id, f"Artist {artist_id}", f"Artist {artist_id}"),
    )
    conn.commit()
    return artist_id


def _insert_work(conn: psycopg.Connection, work_id: str, artist_id: str) -> str:
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, %s, %s)",
        (work_id, f"Work {work_id}", artist_id),
    )
    conn.commit()
    return work_id


def _insert_recording(
    conn: psycopg.Connection, rec_id: str, work_id: str | None = None
) -> str:
    conn.execute(
        "INSERT INTO recordings (id, title, work_id) VALUES (%s, %s, %s)",
        (rec_id, f"Recording {rec_id}", work_id),
    )
    conn.commit()
    return rec_id


# ---------------------------------------------------------------------------
# Full seed: station → playlist → artist → identity → event
# ---------------------------------------------------------------------------


def _seed_review_chain(
    db_conn: psycopg.Connection,
    artist_name: str = "Review Artist",
    with_candidates: bool = True,
) -> tuple[
    BroadcastStation,
    BroadcastPlaylist,
    BroadcastArtist,
    BroadcastTrackIdentity,
    BroadcastPlayEvent,
]:
    station = _insert_station(db_conn)
    playlist = _insert_playlist(db_conn, station)
    artist = _insert_artist(db_conn, original_name=artist_name, with_candidates=with_candidates)
    identity = _insert_identity(db_conn, artist)
    event = _insert_event(db_conn, playlist, identity)
    return station, playlist, artist, identity, event


# ---------------------------------------------------------------------------
# TestMatchingQueue
# ---------------------------------------------------------------------------


class TestMatchingQueue:
    def test_returns_artists_needing_review(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn, with_candidates=True)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

        item = data["items"][0]
        assert item["id"] == str(artist.id)
        assert item["original_name"] == "Review Artist"
        assert item["match_status"] == "needs_review"
        assert item["candidates"] is not None
        assert len(item["candidates"]) == 1
        assert item["candidates"][0]["mbid"] == "abc-123"

        assert len(item["identities"]) == 1
        qi = item["identities"][0]
        assert qi["id"] == str(identity.id)
        assert qi["original_title"] == "Test Song"
        assert qi["match_status"] == "pending"
        assert qi["match_tier"] is None

    def test_empty_queue(self, client, db_conn):
        # Insert an artist that is already AUTO_MATCHED (not in queue)
        _insert_artist(db_conn, match_status=MatchStatus.AUTO_MATCHED)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_pagination(self, client, db_conn):
        for i in range(3):
            _insert_artist(db_conn, original_name=f"Artist {i}")

        resp = client.get("/api/v1/matching/queue?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_pending_artists_included(self, client, db_conn):
        _insert_artist(db_conn, match_status=MatchStatus.PENDING)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# TestResolveArtist
# ---------------------------------------------------------------------------


class TestResolveArtist:
    def test_manual_match(self, client, db_conn):
        _, _, artist, _, _ = _seed_review_chain(db_conn)
        target_mbid = "artist-mbid-0001"

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_matched", "target_artist_id": target_mbid},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(artist.id)
        assert data["match_status"] == "manual_matched"

        # Verify DB state: broadcast_artists updated
        row = db_conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s", (artist.id,)
        ).fetchone()
        assert row is not None
        assert row["match_status"] == "manual_matched"

        # Verify match row created
        match_row = db_conn.execute(
            "SELECT * FROM matches WHERE artist_id = %s", (artist.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["target_id"] == target_mbid
        assert match_row["target_type"] == "artist"
        assert match_row["confidence_score"] == 1.0
        assert match_row["match_tier"] == "manual"

    def test_manual_reject_cascades(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "manual_rejected"

        # Artist updated
        artist_row = db_conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s", (artist.id,)
        ).fetchone()
        assert artist_row is not None
        assert artist_row["match_status"] == "manual_rejected"

        # Child identity cascaded to AUTO_REJECTED
        identity_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s", (identity.id,)
        ).fetchone()
        assert identity_row is not None
        assert identity_row["match_status"] == "auto_rejected"

    def test_manual_reject_does_not_cascade_protected(self, client, db_conn):
        """MANUAL_MATCHED/MANUAL_REJECTED child identities must not be overwritten."""
        _, _, artist, _, _ = _seed_review_chain(db_conn)
        # Add a second identity that is already MANUAL_MATCHED
        protected = _insert_identity(
            db_conn, artist, original_title="Protected Song",
            match_status=MatchStatus.MANUAL_MATCHED,
        )

        client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_rejected"},
        )

        protected_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s", (protected.id,)
        ).fetchone()
        assert protected_row is not None
        assert protected_row["match_status"] == "manual_matched"

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/matching/artists/{uuid4()}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 404

    def test_invalid_status(self, client, db_conn):
        _, _, artist, _, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "pending"},
        )
        assert resp.status_code == 422

    def test_manual_matched_requires_target(self, client, db_conn):
        _, _, artist, _, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_matched"},
        )
        assert resp.status_code == 422

    def test_manual_match_resets_review_children_and_enqueues(
        self, client, db_conn, monkeypatch
    ):
        """MANUAL_MATCHED on an artist must (a) reset all review-relevant
        children to PENDING, (b) delete their stale match rows, and (c)
        enqueue identity_matching_task for each affected playlist. This is
        the cascade fix that lets a curator re-link a wrongly auto-matched
        artist and have its children re-match against the corrected target.
        Bug C — paired with the visibility fix."""
        # Seed:
        #   - AUTO_MATCHED parent
        #   - NEEDS_REVIEW child (raw insert with match_tier/reason_code/
        #     reason_detail populated, mirroring the real Saliva/"Your Disease"
        #     row) + matches row
        #   - PENDING child + matches row (verifies broader DELETE scope)
        #   - station, playlist, one play_event per child
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station)
        artist = _insert_artist(
            db_conn,
            original_name="Saliva",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        review_child = BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Your Disease",
            normalized_title="your disease",
            normalized_signature=f"{artist.normalized_name}:your disease:{uuid4().hex}",
            match_status=MatchStatus.NEEDS_REVIEW,
        )
        # Raw insert so we can populate match_tier / reason_code / reason_detail
        # — the upsert helper only writes match_status, leaving these NULL by
        # default. The cascade clears them, so they need to start non-NULL for
        # the assertion to be meaningful.
        db_conn.execute(
            """
            INSERT INTO track_identities (
                id, broadcast_artist_id, original_title, normalized_title,
                normalized_signature, match_status, match_tier,
                reason_code, reason_detail
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                review_child.id,
                review_child.broadcast_artist_id,
                review_child.original_title,
                review_child.normalized_title,
                review_child.normalized_signature,
                review_child.match_status.value,
                "musicbrainz_id_search",
                "AMBIGUOUS_GAP",
                "Top candidates within 0 points (gap < 10 required)",
            ),
        )
        db_conn.commit()
        _insert_match_row(db_conn, review_child, confidence_score=83.0)
        pending_child = _insert_identity(
            db_conn, artist,
            original_title="Doperide",
            match_status=MatchStatus.PENDING,
        )
        _insert_match_row(db_conn, pending_child, confidence_score=42.0)
        _insert_event(db_conn, playlist, review_child)
        _insert_event(db_conn, playlist, pending_child)

        # Stub the enqueue so the test doesn't depend on Huey worker state.
        # No raising=False — if the dotted path is wrong, fail loudly.
        mock = MagicMock()
        monkeypatch.setattr(
            "backend.routers.matching.identity_matching_task", mock
        )

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={
                "match_status": "manual_matched",
                "target_artist_id": "6e650a01-6489-4dc8-85e1-8ec809dd72a2",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "manual_matched"

        # (1) Task enqueued for the playlist that contained either child.
        mock.assert_called_once_with(str(playlist.id))

        # (2) Both children flipped to PENDING (review_child was NEEDS_REVIEW;
        #     pending_child was already PENDING — UPDATE is a no-op for it but
        #     still in the affected set).
        rows = db_conn.execute(
            "SELECT id, match_status, match_tier, reason_code, reason_detail "
            "FROM track_identities WHERE broadcast_artist_id = %s "
            "ORDER BY original_title",
            (artist.id,),
        ).fetchall()
        assert {r["match_status"] for r in rows} == {"pending"}
        # match_tier / reason_* must be cleared on the formerly-NEEDS_REVIEW
        # child so it doesn't carry stale matcher state into the next run.
        assert all(r["match_tier"] is None for r in rows)
        assert all(r["reason_code"] is None for r in rows)
        assert all(r["reason_detail"] is None for r in rows)

        # (3) Stale identity-keyed match rows deleted (both children, not just
        #     the formerly-NEEDS_REVIEW one).
        identity_match_rows = db_conn.execute(
            "SELECT * FROM matches WHERE identity_id = ANY(%s)",
            ([review_child.id, pending_child.id],),
        ).fetchall()
        assert identity_match_rows == []

        # (4) The artist-keyed match row from the upsert is present.
        artist_match = db_conn.execute(
            "SELECT target_id, target_type, match_tier "
            "FROM matches WHERE artist_id = %s",
            (artist.id,),
        ).fetchone()
        assert artist_match is not None
        assert artist_match["target_id"] == "6e650a01-6489-4dc8-85e1-8ec809dd72a2"
        assert artist_match["target_type"] == "artist"
        assert artist_match["match_tier"] == "manual"

    def test_manual_match_commits_before_enqueue(
        self, client, db_conn, migrated_db, monkeypatch
    ):
        """The cascade must commit BEFORE enqueueing identity_matching_task.
        Otherwise a fast Huey worker (separate process, separate connection)
        could pick up the task and read stale data — children still
        NEEDS_REVIEW, not PENDING — and skip them entirely (the worker's
        get_pending_for_playlist filters strictly to match_status='pending').

        Locks in the ordering invariant: at the moment identity_matching_task
        is invoked, an independent connection must see the cascade results.
        Regression check raised in PR #46 review."""
        from psycopg.rows import dict_row

        artist = _insert_artist(
            db_conn,
            original_name="Saliva",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        review_child = _insert_identity(
            db_conn, artist,
            original_title="Your Disease",
            match_status=MatchStatus.NEEDS_REVIEW,
        )
        _insert_match_row(db_conn, review_child, confidence_score=83.0)
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station)
        _insert_event(db_conn, playlist, review_child)

        observed: list[tuple[str | None, int]] = []

        def capture_visible_state(_playlist_id: str) -> None:
            # Open a fresh connection to simulate the Huey worker process.
            # If the request handler has already committed, the new state is
            # visible; otherwise the seeded state is.
            with psycopg.connect(migrated_db, row_factory=dict_row) as fresh:
                row = fresh.execute(
                    "SELECT match_status FROM track_identities WHERE id = %s",
                    (review_child.id,),
                ).fetchone()
                match_count = fresh.execute(
                    "SELECT COUNT(*)::int AS n FROM matches WHERE identity_id = %s",
                    (review_child.id,),
                ).fetchone()
            observed.append(
                (row["match_status"] if row else None, match_count["n"] if match_count else -1)
            )

        mock = MagicMock(side_effect=capture_visible_state)
        monkeypatch.setattr(
            "backend.routers.matching.identity_matching_task", mock
        )

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={
                "match_status": "manual_matched",
                "target_artist_id": "6e650a01-6489-4dc8-85e1-8ec809dd72a2",
            },
        )
        assert resp.status_code == 200

        assert mock.call_count == 1
        assert observed == [("pending", 0)], (
            "Worker view at enqueue time must reflect the committed cascade "
            f"(pending + 0 matches), not the pre-commit state. Got {observed!r}."
        )

    def test_manual_match_returns_200_when_enqueue_fails(
        self, client, db_conn, monkeypatch
    ):
        """If identity_matching_task raises (Huey backend down, file lock,
        etc.) AFTER the cascade has committed, the request must still return
        200 — the user's manual link succeeded, the children are durably
        flipped to PENDING, and the next /matching/run cycle will pick them
        up. Returning 500 would tell the curator their link failed when it
        actually succeeded, prompting confusing retries.

        Regression check raised in PR #46 review (severity: low)."""
        artist = _insert_artist(
            db_conn,
            original_name="Saliva",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        review_child = _insert_identity(
            db_conn, artist,
            original_title="Your Disease",
            match_status=MatchStatus.NEEDS_REVIEW,
        )
        _insert_match_row(db_conn, review_child, confidence_score=83.0)
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station)
        _insert_event(db_conn, playlist, review_child)

        mock = MagicMock(side_effect=RuntimeError("huey backend down"))
        monkeypatch.setattr(
            "backend.routers.matching.identity_matching_task", mock
        )

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={
                "match_status": "manual_matched",
                "target_artist_id": "6e650a01-6489-4dc8-85e1-8ec809dd72a2",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "manual_matched"

        # Cascade is committed even though the enqueue raised.
        artist_row = db_conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s",
            (artist.id,),
        ).fetchone()
        assert artist_row is not None
        assert artist_row["match_status"] == "manual_matched"

        child_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s",
            (review_child.id,),
        ).fetchone()
        assert child_row is not None
        assert child_row["match_status"] == "pending"

        # The mock was actually called (not bypassed); it just raised.
        mock.assert_called_once_with(str(playlist.id))

    def test_manual_match_does_not_touch_resolved_children(
        self, client, db_conn, monkeypatch
    ):
        """MANUAL_MATCHED cascade must leave AUTO_MATCHED, MANUAL_MATCHED,
        AUTO_REJECTED, and MANUAL_REJECTED children alone. Mirrors the
        existing test_manual_reject_does_not_cascade_protected invariant."""
        artist = _insert_artist(
            db_conn,
            original_name="Saliva",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        # One review-relevant child to trigger the cascade.
        review_child = _insert_identity(
            db_conn, artist,
            original_title="Your Disease",
            match_status=MatchStatus.NEEDS_REVIEW,
        )
        # Resolved siblings — must remain untouched.
        auto_matched_child = _insert_identity(
            db_conn, artist,
            original_title="Click Click Boom",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        manual_matched_child = _insert_identity(
            db_conn, artist,
            original_title="Always",
            match_status=MatchStatus.MANUAL_MATCHED,
        )
        auto_rejected_child = _insert_identity(
            db_conn, artist,
            original_title="Faultline",
            match_status=MatchStatus.AUTO_REJECTED,
        )
        manual_rejected_child = _insert_identity(
            db_conn, artist,
            original_title="Beg",
            match_status=MatchStatus.MANUAL_REJECTED,
        )

        # Need at least one play_event so the playlist lookup is non-empty
        # (otherwise the cascade enqueue runs zero times — fine for this test
        # but it doesn't exercise the resolved-child guard).
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station)
        _insert_event(db_conn, playlist, review_child)

        mock = MagicMock()
        monkeypatch.setattr(
            "backend.routers.matching.identity_matching_task", mock
        )

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={
                "match_status": "manual_matched",
                "target_artist_id": "6e650a01-6489-4dc8-85e1-8ec809dd72a2",
            },
        )
        assert resp.status_code == 200

        # Resolved children must keep their original status.
        for child, expected in [
            (auto_matched_child, "auto_matched"),
            (manual_matched_child, "manual_matched"),
            (auto_rejected_child, "auto_rejected"),
            (manual_rejected_child, "manual_rejected"),
        ]:
            row = db_conn.execute(
                "SELECT match_status FROM track_identities WHERE id = %s",
                (child.id,),
            ).fetchone()
            assert row is not None
            assert row["match_status"] == expected, (
                f"{child.original_title}: expected {expected}, got {row['match_status']}"
            )

        # Review child flipped to pending.
        review_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s",
            (review_child.id,),
        ).fetchone()
        assert review_row is not None
        assert review_row["match_status"] == "pending"

    def test_manual_match_accepts_local_catalog_uuid(self, client, db_conn):
        """target_artist_id accepts a local catalog UUID, not just an MBID.

        The auto-matcher writes local artist UUIDs for local-only canonicals
        (artists.mbid IS NULL) via the NormalizationStrategy exact-pass at
        artist_matching_service.py.  Manual resolution from the library-search
        slide-over sends the same form (mbid?.trim() ? mbid : id), so the
        endpoint must store whatever text value is supplied without validation
        of its shape.
        """
        _, _, artist, _, _ = _seed_review_chain(db_conn)
        local_catalog_id = str(uuid4())  # UUID string, not an MBID

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_matched", "target_artist_id": local_catalog_id},
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "manual_matched"

        match_row = db_conn.execute(
            "SELECT target_id, match_tier, confidence_score FROM matches WHERE artist_id = %s",
            (artist.id,),
        ).fetchone()
        assert match_row is not None
        assert match_row["target_id"] == local_catalog_id
        assert match_row["match_tier"] == "manual"
        assert match_row["confidence_score"] == 1.0


# ---------------------------------------------------------------------------
# TestResolveIdentity
# ---------------------------------------------------------------------------


class TestResolveIdentity:
    def test_manual_match(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        lib_file = _insert_library_file(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_matched", "library_file_id": str(lib_file.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(identity.id)
        assert data["match_status"] == "manual_matched"

        # Verify track_identity updated with MANUAL tier
        id_row = db_conn.execute(
            "SELECT match_status, match_tier FROM track_identities WHERE id = %s",
            (identity.id,),
        ).fetchone()
        assert id_row is not None
        assert id_row["match_status"] == "manual_matched"
        assert id_row["match_tier"] == "manual"

        # Verify match row created
        match_row = db_conn.execute(
            "SELECT * FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["library_file_id"] == lib_file.id
        assert match_row["target_type"] == "library_file"
        assert match_row["confidence_score"] == 1.0
        assert match_row["match_tier"] == "manual"

    def test_manual_reject(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["match_status"] == "manual_rejected"

        id_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s", (identity.id,)
        ).fetchone()
        assert id_row is not None
        assert id_row["match_status"] == "manual_rejected"

    def test_manual_match_replaces_existing_match(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        lib_file_old = _insert_library_file(db_conn)
        lib_file_new = _insert_library_file(db_conn)

        # Create an initial match row
        db_conn.execute(
            """
            INSERT INTO matches (
                id, identity_id, library_file_id, target_type, confidence_score, match_tier
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                identity.id,
                lib_file_old.id,
                "library_file",
                0.85,
                "vector",
            ),
        )
        db_conn.commit()

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_matched", "library_file_id": str(lib_file_new.id)},
        )
        assert resp.status_code == 200

        # Only one match row should exist and it should be the new one
        rows = db_conn.execute(
            "SELECT * FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["library_file_id"] == lib_file_new.id
        assert rows[0]["match_tier"] == "manual"

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/matching/identities/{uuid4()}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 404

    def test_invalid_status(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "auto_matched"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestResolveIdentityWorkId — migration 0024 wiring
#
# Pins the manual-resolve work_id derivation (matches.work_id == the picked
# library_file's work_id, or NULL if it is unset — recording_id is NOT a
# fallback because matches.work_id is FK to works(id)), the post-commit
# recalculate_song_masters dispatch, and the failure semantics on both sides
# of the commit boundary.
# ---------------------------------------------------------------------------


class TestResolveIdentityWorkId:
    def test_persists_work_id_from_lib_file_work_id(
        self, client, db_conn, monkeypatch
    ):
        """library_files.work_id is set → matches.work_id matches."""
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        artist_id = _insert_canonical_artist(db_conn, "art-w1")
        work_id = _insert_work(db_conn, "work-w1", artist_id)
        rec_id = _insert_recording(db_conn, "rec-w1", work_id=work_id)
        lib_file = _insert_library_file(
            db_conn, recording_id=rec_id, work_id=work_id
        )

        # Stub recalc — exercise the wiring without re-running master
        # selection in the test path.
        captured: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "backend.routers.matching.recalculate_for_work_sync",
            lambda db_url, w: captured.append((db_url, w)),
        )

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={
                "match_status": "manual_matched",
                "library_file_id": str(lib_file.id),
            },
        )
        assert resp.status_code == 200

        match_row = db_conn.execute(
            "SELECT work_id FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["work_id"] == work_id
        assert [w for _, w in captured] == [work_id]

    def test_persists_null_work_id_when_only_recording_id_present(
        self, client, db_conn, monkeypatch
    ):
        """library_files.work_id NULL with only recording_id set → matches.work_id
        is NULL and recalc is skipped.

        matches.work_id is FK to works(id); a recording_id (which lives in
        recordings(id)) would fail the FK on insert. The auto-matcher used
        to surface recording_id as a stand-in work_id, but that was always a
        no-op in recalculate_song_masters and is now an active correctness
        constraint at persistence time — NULL is the right value when the
        actual work is unknown.
        """
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        rec_id = "rec-only-no-work"
        _insert_recording(db_conn, rec_id, work_id=None)
        lib_file = _insert_library_file(
            db_conn, recording_id=rec_id, work_id=None
        )

        captured: list[str] = []
        monkeypatch.setattr(
            "backend.routers.matching.recalculate_for_work_sync",
            lambda db_url, w: captured.append(w),
        )

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={
                "match_status": "manual_matched",
                "library_file_id": str(lib_file.id),
            },
        )
        assert resp.status_code == 200

        match_row = db_conn.execute(
            "SELECT work_id FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["work_id"] is None
        assert captured == []

    def test_persists_null_work_id_and_skips_recalc_when_neither_present(
        self, client, db_conn, monkeypatch
    ):
        """Neither work_id nor recording_id on the file → matches.work_id is
        NULL and the recalc dispatch is skipped (no work to recompute).
        """
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        lib_file = _insert_library_file(db_conn)

        captured: list[str] = []
        monkeypatch.setattr(
            "backend.routers.matching.recalculate_for_work_sync",
            lambda db_url, w: captured.append(w),
        )

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={
                "match_status": "manual_matched",
                "library_file_id": str(lib_file.id),
            },
        )
        assert resp.status_code == 200

        match_row = db_conn.execute(
            "SELECT work_id FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["work_id"] is None
        assert captured == []

    def test_recalc_failure_does_not_500(self, client, db_conn, monkeypatch):
        """Side-effect failure post-commit: response is still 200, the match
        row is durable, status flipped to manual_matched, warning logged.
        """
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        artist_id = _insert_canonical_artist(db_conn, "art-fail")
        work_id = _insert_work(db_conn, "work-fail", artist_id)
        rec_id = _insert_recording(db_conn, "rec-fail", work_id=work_id)
        lib_file = _insert_library_file(
            db_conn, recording_id=rec_id, work_id=work_id
        )

        def _boom(db_url: str, w: str) -> None:
            raise RuntimeError("recalc exploded")

        monkeypatch.setattr(
            "backend.routers.matching.recalculate_for_work_sync", _boom
        )

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={
                "match_status": "manual_matched",
                "library_file_id": str(lib_file.id),
            },
        )
        assert resp.status_code == 200

        # Match write was durable — recalc failure must not roll it back.
        match_row = db_conn.execute(
            "SELECT work_id, library_file_id FROM matches WHERE identity_id = %s",
            (identity.id,),
        ).fetchone()
        assert match_row is not None
        assert match_row["work_id"] == work_id
        assert match_row["library_file_id"] == lib_file.id

        id_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s",
            (identity.id,),
        ).fetchone()
        assert id_row is not None
        assert id_row["match_status"] == "manual_matched"

    def test_missing_library_file_id_returns_422(self, client, db_conn):
        """MANUAL_MATCHED without library_file_id is a contract violation —
        422 from the validator, no match row written.
        """
        _, _, _, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_matched"},
        )
        assert resp.status_code == 422

        row = db_conn.execute(
            "SELECT 1 FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert row is None

    def test_unknown_library_file_id_returns_422_and_aborts(
        self, client, db_conn
    ):
        """library_file_id pointing at a non-existent row → deterministic 422
        from LibraryFileNotFoundError; the surrounding async transaction
        rolls back so neither the status update nor the prior-match DELETE
        is observable.
        """
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        bogus_id = uuid4()

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={
                "match_status": "manual_matched",
                "library_file_id": str(bogus_id),
            },
        )
        assert resp.status_code == 422

        match_row = db_conn.execute(
            "SELECT 1 FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is None

        id_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s",
            (identity.id,),
        ).fetchone()
        assert id_row is not None
        # The pre-existing seed status should NOT have flipped to manual_matched.
        assert id_row["match_status"] != "manual_matched"


# ---------------------------------------------------------------------------
# TestMatchingRun
# ---------------------------------------------------------------------------


class TestMatchingRun:
    def test_accepted(self, client):
        resp = client.post("/api/v1/matching/run")
        assert resp.status_code == 202

    def test_returns_count(self, client, db_conn):
        # Seed two playlists with unresolved artists (distinct stations to avoid unique violation)
        for i in range(2):
            station = _insert_station(db_conn, call_letters=f"KRUN-{i}")
            playlist = _insert_playlist(db_conn, station, name=f"run_{i}.csv")
            artist = _insert_artist(db_conn, original_name=f"Run Artist {i}")
            identity = _insert_identity(db_conn, artist)
            _insert_event(db_conn, playlist, identity)

        resp = client.post("/api/v1/matching/run")
        assert resp.status_code == 202
        data = resp.json()
        assert "count" in data
        assert isinstance(data["count"], int)
        assert data["count"] >= 1


# ---------------------------------------------------------------------------
# TestComputeTriageBucket  (pure unit — no DB required)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (None, "blocked"),
        (0.0, "blocked"),
        (49.9, "blocked"),
        (MIN_PRESENTATION_SCORE, "needs_attention"),  # 50.0 — boundary
        (50.0, "needs_attention"),
        (54.9, "needs_attention"),
        (64.9, "needs_attention"),
        (65.0, "quick_review"),                       # boundary
        (79.9, "quick_review"),
        (80.0, "quick_review"),
        (99.9, "quick_review"),                       # in queue = not auto-matched
    ],
)
def test_compute_triage_bucket_boundary(score: float | None, expected: str) -> None:
    assert _compute_triage_bucket(score) == expected


# ---------------------------------------------------------------------------
# TestArtistBucketReduction  (pure unit — no DB required)
# ---------------------------------------------------------------------------


def _identity(bucket: str, match_status: str = "needs_review") -> QueueIdentity:
    return QueueIdentity(
        id=uuid4(),
        original_title="x",
        normalized_title="x",
        match_status=match_status,
        match_tier=None,
        confidence_score=None,
        triage_bucket=bucket,  # type: ignore[arg-type]
    )


class TestArtistBucketReduction:
    def test_empty_returns_blocked(self) -> None:
        assert _artist_bucket_from_identities([]) == "blocked"

    def test_any_quick_review_wins(self) -> None:
        ids = [
            _identity("needs_attention"),
            _identity("quick_review"),
            _identity("blocked"),
        ]
        assert _artist_bucket_from_identities(ids) == "quick_review"

    def test_needs_attention_wins_over_blocked(self) -> None:
        ids = [_identity("blocked"), _identity("needs_attention")]
        assert _artist_bucket_from_identities(ids) == "needs_attention"

    def test_all_blocked_returns_blocked(self) -> None:
        ids = [_identity("blocked"), _identity("blocked")]
        assert _artist_bucket_from_identities(ids) == "blocked"

    def test_resolved_identities_do_not_inflate_bucket(self) -> None:
        # AUTO_MATCHED + MANUAL_* + *_REJECTED children represent completed
        # curator work and must not contaminate the artist-level headline.
        ids = [
            _identity("quick_review", match_status="auto_matched"),
            _identity("quick_review", match_status="manual_matched"),
            _identity("blocked", match_status="auto_rejected"),
            _identity("blocked", match_status="needs_review"),
        ]
        assert _artist_bucket_from_identities(ids) == "blocked"

    def test_all_children_resolved_returns_blocked(self) -> None:
        # No review-relevant children at all → "blocked" (same as empty list).
        ids = [
            _identity("quick_review", match_status="auto_matched"),
            _identity("quick_review", match_status="manual_matched"),
        ]
        assert _artist_bucket_from_identities(ids) == "blocked"

    def test_review_relevant_identity_still_drives_bucket(self) -> None:
        ids = [
            _identity("quick_review", match_status="auto_matched"),
            _identity("needs_attention", match_status="pending"),
        ]
        assert _artist_bucket_from_identities(ids) == "needs_attention"


# ---------------------------------------------------------------------------
# TestQueueResponseShape  (integration — requires DB)
# ---------------------------------------------------------------------------


def _insert_match_row(
    conn: psycopg.Connection,
    identity: BroadcastTrackIdentity,
    confidence_score: float,
    library_file_id: UUID | None = None,
    match_tier: str = "vector",
) -> None:
    """Insert a matches row for the given identity.

    library_file_id defaults to None to preserve the original behavior — every
    pre-existing callsite continues to insert a NULL FK. Pass an explicit UUID
    (e.g. from _insert_library_file) when a test asserts on `proposed_match`.
    """
    conn.execute(
        """
        INSERT INTO matches (
            id, identity_id, library_file_id, target_type,
            confidence_score, match_tier
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            uuid4(),
            identity.id,
            library_file_id,
            "library_file",
            confidence_score,
            match_tier,
        ),
    )
    conn.commit()


class TestQueueResponseShape:
    def test_triage_bucket_quick_review_from_confidence(self, client, db_conn):
        """Identity with confidence_score=72.0 → quick_review bucket."""
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        _insert_match_row(db_conn, identity, confidence_score=72.0)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()

        assert len(data["items"]) == 1
        item = data["items"][0]
        qi = item["identities"][0]
        assert qi["confidence_score"] == pytest.approx(72.0, abs=1e-4)
        assert qi["triage_bucket"] == "quick_review"
        assert item["triage_bucket"] == "quick_review"

    def test_triage_bucket_needs_attention(self, client, db_conn):
        """Identity with confidence_score=55.0 → needs_attention bucket."""
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        _insert_match_row(db_conn, identity, confidence_score=55.0)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["identities"][0]["triage_bucket"] == "needs_attention"
        assert item["triage_bucket"] == "needs_attention"

    def test_triage_bucket_blocked_no_match(self, client, db_conn):
        """Identity with no matches row → confidence_score=None → blocked."""
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        qi = item["identities"][0]
        assert qi["confidence_score"] is None
        assert qi["triage_bucket"] == "blocked"
        assert item["triage_bucket"] == "blocked"

    def test_distinct_on_prevents_row_multiplication(self, client, db_conn):
        """DISTINCT ON: 3 matches rows for 1 identity must produce 1 identity with
        the highest-confidence row selected."""
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        # _insert_match_row commits each row, so these three match rows have
        # distinct created_at timestamps; the DISTINCT ON primary key is
        # confidence_score DESC, so 72.0 wins deterministically.
        for score in (72.0, 60.0, 45.0):
            _insert_match_row(db_conn, identity, confidence_score=score)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert len(item["identities"]) == 1
        assert item["identities"][0]["confidence_score"] == 72.0
        assert item["identities"][0]["triage_bucket"] == "quick_review"

    def test_reason_code_detail_propagated(self, client, db_conn):
        """reason_code + reason_detail appear on both identity and artist."""
        # Insert artist with reason columns
        db_conn.execute(
            """
            INSERT INTO broadcast_artists (
                id, original_name, normalized_name, match_status,
                reason_code, reason_detail
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                (aid := uuid4()),
                "Reason Artist",
                "reason artist",
                "needs_review",
                "NO_CANDIDATES",
                "No MusicBrainz candidates found",
            ),
        )
        # Insert identity with reason columns
        db_conn.execute(
            """
            INSERT INTO track_identities (
                id, broadcast_artist_id, original_title, normalized_title,
                normalized_signature, match_status, reason_code, reason_detail
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                aid,
                "Reason Song",
                "reason song",
                f"reason artist:reason song:{uuid4().hex}",
                "needs_review",
                "LOW_CONFIDENCE",
                "Score below threshold",
            ),
        )
        db_conn.commit()

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        items = resp.json()["items"]
        item = next(i for i in items if i["original_name"] == "Reason Artist")
        assert item["reason_code"] == "NO_CANDIDATES"
        assert item["reason_detail"] == "No MusicBrainz candidates found"
        assert item["identities"][0]["reason_code"] == "LOW_CONFIDENCE"
        assert item["identities"][0]["reason_detail"] == "Score below threshold"

    def test_resolved_children_do_not_contaminate_artist_bucket(self, client, db_conn):
        """Integration: AUTO_MATCHED identity at score 72 must not lift a
        NEEDS_REVIEW artist to quick_review. The Python reducer and the SQL
        CTE both need to exclude resolved children. Exercises both paths via
        the unfiltered and bucket=blocked queries."""
        _, _, artist, identity_review, _ = _seed_review_chain(
            db_conn, artist_name="Mixed Children Artist"
        )
        # Low-score review-relevant child → blocked
        _insert_match_row(db_conn, identity_review, confidence_score=45.0)
        # High-score AUTO_MATCHED child (would be "quick_review" if counted)
        identity_matched = _insert_identity(
            db_conn, artist, original_title="Resolved Song",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        _insert_match_row(db_conn, identity_matched, confidence_score=95.0)

        # Python-side bucket computation (no ?bucket=)
        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = next(
            i for i in resp.json()["items"] if i["original_name"] == "Mixed Children Artist"
        )
        assert item["triage_bucket"] == "blocked"

        # SQL-side bucket filter (?bucket=blocked) — the artist must survive
        # the filter, proving the SQL CTE excludes the resolved child.
        resp_filtered = client.get("/api/v1/matching/queue?bucket=blocked")
        assert resp_filtered.status_code == 200
        names = [i["original_name"] for i in resp_filtered.json()["items"]]
        assert "Mixed Children Artist" in names

    def test_bucket_filter_quick_review(self, client, db_conn):
        """bucket=quick_review filters out blocked/needs_attention artists."""
        # Artist 1: quick_review (has match at 72.0)
        _, _, artist1, identity1, _ = _seed_review_chain(db_conn, artist_name="Artist QR")
        _insert_match_row(db_conn, identity1, confidence_score=72.0)

        # Artist 2: blocked (no match)
        _insert_artist(db_conn, original_name="Artist Blocked")

        resp = client.get("/api/v1/matching/queue?bucket=quick_review")
        assert resp.status_code == 200
        data = resp.json()
        # total reflects the filtered bucket count so LIMIT/OFFSET stays coherent
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["triage_bucket"] == "quick_review"

    def test_bucket_filter_blocked(self, client, db_conn):
        """bucket=blocked returns only blocked artists."""
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        # No match → blocked
        _insert_artist(db_conn, original_name="Also Blocked")

        resp = client.get("/api/v1/matching/queue?bucket=blocked")
        assert resp.status_code == 200
        data = resp.json()
        assert all(i["triage_bucket"] == "blocked" for i in data["items"])

    def test_existing_queue_fields_still_present(self, client, db_conn):
        """Regression: original_name, candidates, identities.match_status still returned."""
        _, _, artist, identity, _ = _seed_review_chain(db_conn, with_candidates=True)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["original_name"] == "Review Artist"
        assert item["candidates"] is not None
        assert item["identities"][0]["match_status"] == "pending"

    def test_auto_matched_artist_with_review_child_is_visible(self, client, db_conn):
        """When an artist is AUTO_MATCHED but has a child identity in
        NEEDS_REVIEW, the artist must surface in the queue so the curator can
        resolve the child. Pre-fix the queue CTE filtered artists by
        match_status IN (PENDING, NEEDS_REVIEW), silently hiding such cases.
        Bug A regression — the Saliva/"Your Disease" scenario."""
        artist = _insert_artist(
            db_conn,
            original_name="Saliva",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        _insert_identity(
            db_conn, artist,
            original_title="Your Disease",
            match_status=MatchStatus.NEEDS_REVIEW,
        )

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        items = data["items"]
        assert len(items) == 1
        item = items[0]
        assert item["original_name"] == "Saliva"
        assert item["match_status"] == "auto_matched"
        assert len(item["identities"]) == 1
        qi = item["identities"][0]
        assert qi["original_title"] == "Your Disease"
        assert qi["match_status"] == "needs_review"

    def test_mixed_status_artist_bucket_excludes_resolved_children_when_parent_resolved(
        self, client, db_conn
    ):
        """AUTO_MATCHED parent with one AUTO_MATCHED child (high score) and one
        NEEDS_REVIEW child (low score) — the artist surfaces because of the
        review-relevant child, and its bucket reflects ONLY that child. The
        AUTO_MATCHED child must not inflate the headline to quick_review."""
        artist = _insert_artist(
            db_conn,
            original_name="Resolved Parent Mixed",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        review_child = _insert_identity(
            db_conn, artist,
            original_title="Needs Work",
            match_status=MatchStatus.NEEDS_REVIEW,
        )
        _insert_match_row(db_conn, review_child, confidence_score=45.0)
        resolved_child = _insert_identity(
            db_conn, artist,
            original_title="Already Done",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        _insert_match_row(db_conn, resolved_child, confidence_score=95.0)

        # Python-side bucket
        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = next(
            i for i in resp.json()["items"]
            if i["original_name"] == "Resolved Parent Mixed"
        )
        assert item["triage_bucket"] == "blocked"

        # SQL-side bucket filter: artist must survive ?bucket=blocked
        resp_filtered = client.get("/api/v1/matching/queue?bucket=blocked")
        assert resp_filtered.status_code == 200
        names = [i["original_name"] for i in resp_filtered.json()["items"]]
        assert "Resolved Parent Mixed" in names

    def test_total_includes_auto_matched_artists_with_review_children(
        self, client, db_conn
    ):
        """`total` must count newly-visible AUTO_MATCHED parents whose
        children are review-relevant — this metric drove pagination before
        and pre-fix it silently dropped these artists."""
        # Pre-fix baseline: only an unresolved artist counts
        artist1 = _insert_artist(
            db_conn,
            original_name="Plain Pending",
            match_status=MatchStatus.PENDING,
        )
        _insert_identity(db_conn, artist1, match_status=MatchStatus.PENDING)

        # Newly-visible: AUTO_MATCHED parent with a NEEDS_REVIEW child
        artist2 = _insert_artist(
            db_conn,
            original_name="Auto Parent",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        _insert_identity(
            db_conn, artist2,
            original_title="Child Needs Review",
            match_status=MatchStatus.NEEDS_REVIEW,
        )

        # Childless AUTO_MATCHED artist must still be excluded (regression check
        # for test_empty_queue's invariant — the EXISTS subquery returns false).
        _insert_artist(
            db_conn,
            original_name="Childless Resolved",
            match_status=MatchStatus.AUTO_MATCHED,
        )

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {i["original_name"] for i in data["items"]}
        assert names == {"Plain Pending", "Auto Parent"}


# ---------------------------------------------------------------------------
# TestProposedMatch  (integration — requires DB)
# ---------------------------------------------------------------------------


class TestProposedMatch:
    """`proposed_match` surfaces the best-scoring library_file candidate so a
    curator can approve in one click. Three cases matter:

    1. matches.library_file_id points at a real library_files row → populated
    2. matches.library_file_id IS NULL → None (parity with today's data)
    3. matches.library_file_id is orphan (no joinable row) → None, AND the
       identity itself must still be returned (LEFT JOIN guard)
    """

    def test_populated_when_match_has_library_file(self, client, db_conn):
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        lib_file = _insert_library_file(
            db_conn,
            track_title="Two Skins",
            release_title="Decoded",
            recording_mbid="rec-mbid-0001",
        )
        _insert_match_row(
            db_conn,
            identity,
            confidence_score=78.0,
            library_file_id=lib_file.id,
            match_tier="musicbrainz_id_search",
        )

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        qi = item["identities"][0]

        proposed = qi["proposed_match"]
        assert proposed is not None
        assert proposed["library_file_id"] == str(lib_file.id)
        assert proposed["file_path"] == lib_file.file_path
        assert proposed["track_title"] == "Two Skins"
        assert proposed["release_title"] == "Decoded"
        assert proposed["recording_mbid"] == "rec-mbid-0001"
        assert proposed["candidate_match_tier"] == "musicbrainz_id_search"

    def test_none_when_match_has_null_library_file_id(self, client, db_conn):
        """A matches row without library_file_id (today's default) → None."""
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        _insert_match_row(db_conn, identity, confidence_score=72.0)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        qi = resp.json()["items"][0]["identities"][0]
        assert qi["proposed_match"] is None
        # Confidence is still surfaced so the card can show the score.
        assert qi["confidence_score"] == pytest.approx(72.0, abs=1e-4)

    def test_orphan_library_file_id_does_not_drop_identity(self, client, db_conn):
        """LEFT JOIN guard: even if matches.library_file_id points at a missing
        library_files row, the identity must still be returned (just without a
        proposed_match).

        Migration 0004 adds an FK on matches.library_file_id, so orphans can't
        arise in steady state — but the LEFT JOIN remains defensive against
        future schema changes (e.g. relaxing the FK or adding ON DELETE SET
        NULL plus an out-of-order delete). We exercise the guard by disabling
        FK enforcement for this insert via session_replication_role.
        """
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        orphan_id = uuid4()
        # session_replication_role=replica disables FK trigger enforcement on
        # this connection only; restored to origin immediately after.
        db_conn.execute("SET session_replication_role = replica")
        _insert_match_row(
            db_conn,
            identity,
            confidence_score=55.0,
            library_file_id=orphan_id,
        )
        db_conn.execute("SET session_replication_role = origin")
        db_conn.commit()

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1, "identity must still surface despite orphan FK"
        qi = items[0]["identities"][0]
        assert qi["id"] == str(identity.id)
        assert qi["proposed_match"] is None

    def test_distinct_on_picks_highest_score_with_file(self, client, db_conn):
        """When several matches exist, DISTINCT ON keeps the highest-confidence
        row. The proposed_match must reflect that winning row's library_file."""
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        loser = _insert_library_file(db_conn, track_title="Loser")
        winner = _insert_library_file(db_conn, track_title="Winner")
        # Lower-score row inserted first, then higher-score winner.
        _insert_match_row(
            db_conn, identity, confidence_score=55.0, library_file_id=loser.id
        )
        _insert_match_row(
            db_conn, identity, confidence_score=82.0, library_file_id=winner.id
        )

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        qi = resp.json()["items"][0]["identities"][0]
        assert qi["confidence_score"] == pytest.approx(82.0, abs=1e-4)
        assert qi["proposed_match"]["library_file_id"] == str(winner.id)
        assert qi["proposed_match"]["track_title"] == "Winner"

    def test_wrong_artist_match_is_masked_to_none(self, client, db_conn):
        """Locked-artist invariant: a persisted match pointing at a library
        file whose normalized_artist_name disagrees with the broadcast
        artist's normalized_name must surface as proposed_match=None. The
        identity itself stays in the queue (LEFT JOIN, not INNER); the
        cross-artist file is suppressed.

        Regression test for the original Hendrix/U2 bug — the data shape
        the bug produced (matches.library_file_id pointing at a U2 file
        under an artist named Jimi Hendrix) is replayed here.
        """
        _, _, artist, identity, _ = _seed_review_chain(
            db_conn, artist_name="Jimi Hendrix",
        )
        # Cross-artist library file (U2's "Star Spangled Banner") seeded
        # under a Jimi Hendrix identity — exactly what the buggy matcher
        # could produce before this fix.
        wrong_artist_file = _insert_library_file(
            db_conn,
            track_title="The Star Spangled Banner",
            artist_name="U2",
            normalized_artist_name="u2",
        )
        _insert_match_row(
            db_conn,
            identity,
            confidence_score=77.0,
            library_file_id=wrong_artist_file.id,
            match_tier="local_file_fuzzy",
        )

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1, "identity stays visible — only proposal masked"
        qi = items[0]["identities"][0]
        assert qi["id"] == str(identity.id)
        assert qi["proposed_match"] is None
        # confidence_score still surfaces from the matches row so the curator
        # can see the matcher tried; only the file pointer is suppressed.
        assert qi["confidence_score"] == pytest.approx(77.0, abs=1e-4)

    def test_approve_via_resolve_identity_uses_queue_proposed_match(
        self, client, db_conn
    ):
        """End-to-end Approve flow: read library_file_id from /queue and POST it
        to /resolve. The resulting matches row must point at the same file with
        tier=manual / score=1.0."""
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        lib_file = _insert_library_file(db_conn, track_title="Approve Me")
        _insert_match_row(
            db_conn,
            identity,
            confidence_score=78.0,
            library_file_id=lib_file.id,
            match_tier="musicbrainz_id_search",
        )

        # Read the proposed match the way the UI does.
        queue_resp = client.get("/api/v1/matching/queue")
        proposed = queue_resp.json()["items"][0]["identities"][0]["proposed_match"]
        assert proposed is not None

        # Approve: POST that library_file_id back to /resolve.
        resolve_resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={
                "match_status": "manual_matched",
                "library_file_id": proposed["library_file_id"],
            },
        )
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["match_status"] == "manual_matched"

        # Final state: matches row replaced (delete + insert) and points at the
        # same file the curator saw on the card.
        rows = db_conn.execute(
            "SELECT library_file_id, match_tier, confidence_score "
            "FROM matches WHERE identity_id = %s",
            (identity.id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["library_file_id"] == lib_file.id
        assert rows[0]["match_tier"] == "manual"
        assert rows[0]["confidence_score"] == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# TestSearchMbArtists  (unit — FakeMbClient injected via dependency_overrides)
# ---------------------------------------------------------------------------


class TestSearchMbArtists:
    """Tests for GET /api/v1/matching/mb-artists.

    Uses FakeMbClient injected via dependency_overrides so no real MusicBrainz
    network calls or sync DB connections are made.
    """

    def _override_mb(self, app: object, fake: object) -> None:
        from backend.dependencies import get_mb_client
        from backend.main import app as fastapi_app  # type: ignore[attr-defined]

        def _fake_mb() -> object:
            yield fake

        fastapi_app.dependency_overrides[get_mb_client] = _fake_mb  # type: ignore[attr-defined]

    def _clear_mb(self) -> None:
        from backend.dependencies import get_mb_client
        from backend.main import app as fastapi_app  # type: ignore[attr-defined]

        fastapi_app.dependency_overrides.pop(get_mb_client, None)  # type: ignore[attr-defined]

    def test_mb_artists_endpoint_returns_items(self, client) -> None:
        """Stub MB client returns 2 artists; response JSON has items list."""
        from tests.fakes.mb_client import FakeMbClient

        fake = FakeMbClient(
            responses={
                "The Beatles": [
                    {"id": "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d", "name": "The Beatles",
                     "sort-name": "Beatles, The", "score": 100},
                    {"id": "fake-id-2", "name": "Beatles Cover Band",
                     "sort-name": "Beatles Cover Band", "score": 72},
                ]
            }
        )
        self._override_mb(None, fake)
        try:
            resp = client.get("/api/v1/matching/mb-artists?query=The+Beatles")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 2
            assert data["items"][0]["id"] == "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"
            assert data["items"][0]["name"] == "The Beatles"
            assert data["items"][1]["score"] == 72
        finally:
            self._clear_mb()

    def test_mb_artists_requires_non_empty_query(self, client) -> None:
        """Empty query string → 422 Unprocessable Entity."""
        resp = client.get("/api/v1/matching/mb-artists?query=")
        assert resp.status_code == 422

    def test_mb_artists_max_length(self, client) -> None:
        """Query longer than 100 characters → 422 Unprocessable Entity."""
        long_query = "a" * 101
        resp = client.get(f"/api/v1/matching/mb-artists?query={long_query}")
        assert resp.status_code == 422
