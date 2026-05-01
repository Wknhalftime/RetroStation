from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest

from backend.domain.enums import MatchStatus
from tests.routers.test_matching import (
    _insert_artist,
    _insert_identity,
    _seed_review_chain,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_artist_match(
    conn: psycopg.Connection, artist_id: UUID, target_id: str = "artist-mbid-xxx"
) -> UUID:
    match_id = uuid4()
    conn.execute(
        """
        INSERT INTO matches
            (id, artist_id, target_id, target_type, confidence_score, match_tier)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (match_id, artist_id, target_id, "artist", 1.0, "manual"),
    )
    conn.commit()
    return match_id


def _insert_identity_match(
    conn: psycopg.Connection, identity_id: UUID, library_file_id: UUID | None = None
) -> UUID:
    match_id = uuid4()
    conn.execute(
        """
        INSERT INTO matches
            (id, identity_id, library_file_id, target_type, confidence_score, match_tier)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (match_id, identity_id, library_file_id, "library_file", 0.95, "manual"),
    )
    conn.commit()
    return match_id


def _set_artist_status(
    conn: psycopg.Connection, artist_id: UUID, status: MatchStatus
) -> None:
    conn.execute(
        "UPDATE broadcast_artists SET match_status = %s WHERE id = %s",
        (status.value, artist_id),
    )
    conn.commit()


def _set_identity_status(
    conn: psycopg.Connection, identity_id: UUID, status: MatchStatus
) -> None:
    conn.execute(
        "UPDATE track_identities SET match_status = %s WHERE id = %s",
        (status.value, identity_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# TestUnmatchIdentity
# ---------------------------------------------------------------------------


class TestUnmatchIdentity:
    @pytest.mark.parametrize(
        "source_status",
        [
            MatchStatus.AUTO_MATCHED,
            MatchStatus.MANUAL_MATCHED,
            MatchStatus.AUTO_REJECTED,
            MatchStatus.MANUAL_REJECTED,
        ],
    )
    def test_unmatch_finalized_identity(self, client, db_conn, source_status):
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        _set_identity_status(db_conn, identity.id, source_status)
        _insert_identity_match(db_conn, identity.id)

        resp = client.post(f"/api/v1/matching/identities/{identity.id}/unmatch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(identity.id)
        assert data["match_status"] == "needs_review"

        row = db_conn.execute(
            "SELECT match_status, match_tier, reason_code, reason_detail "
            "FROM track_identities WHERE id = %s",
            (identity.id,),
        ).fetchone()
        assert row is not None
        assert row["match_status"] == "needs_review"
        assert row["match_tier"] is None
        assert row["reason_code"] == "USER_UNMATCHED"
        assert row["reason_detail"] is None

        match_row = db_conn.execute(
            "SELECT id FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is None

    @pytest.mark.parametrize(
        "blocked_status",
        [MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW],
    )
    def test_unmatch_non_finalized_returns_409(self, client, db_conn, blocked_status):
        _, _, _, identity, _ = _seed_review_chain(db_conn)
        _set_identity_status(db_conn, identity.id, blocked_status)

        resp = client.post(f"/api/v1/matching/identities/{identity.id}/unmatch")
        assert resp.status_code == 409

    def test_unmatch_unknown_identity_returns_404(self, client):
        resp = client.post(f"/api/v1/matching/identities/{uuid4()}/unmatch")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestUnmatchArtist
# ---------------------------------------------------------------------------


class TestUnmatchArtist:
    @pytest.mark.parametrize(
        "source_status",
        [
            MatchStatus.AUTO_MATCHED,
            MatchStatus.MANUAL_MATCHED,
            MatchStatus.AUTO_REJECTED,
            MatchStatus.MANUAL_REJECTED,
        ],
    )
    def test_unmatch_finalized_artist(self, client, db_conn, source_status):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        _set_artist_status(db_conn, artist.id, source_status)
        _insert_artist_match(db_conn, artist.id)

        resp = client.post(f"/api/v1/matching/artists/{artist.id}/unmatch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(artist.id)
        assert data["match_status"] == "needs_review"

        artist_row = db_conn.execute(
            "SELECT match_status, reason_code, reason_detail "
            "FROM broadcast_artists WHERE id = %s",
            (artist.id,),
        ).fetchone()
        assert artist_row is not None
        assert artist_row["match_status"] == "needs_review"
        assert artist_row["reason_code"] == "USER_UNMATCHED"
        assert artist_row["reason_detail"] is None

        identity_row = db_conn.execute(
            "SELECT match_status, match_tier, reason_code "
            "FROM track_identities WHERE id = %s",
            (identity.id,),
        ).fetchone()
        assert identity_row is not None
        assert identity_row["match_status"] == "needs_review"
        assert identity_row["match_tier"] is None
        assert identity_row["reason_code"] == "USER_UNMATCHED"

        artist_match = db_conn.execute(
            "SELECT id FROM matches WHERE artist_id = %s", (artist.id,)
        ).fetchone()
        assert artist_match is None

    def test_artist_unmatch_cascades_all_children_regardless_of_status(
        self, client, db_conn
    ):
        """Distinct from resolve_artist's MANUAL_REJECTED cascade — unmatch wipes
        even MANUAL_MATCHED / MANUAL_REJECTED children.
        """
        _, _, artist, default_identity, _ = _seed_review_chain(db_conn)
        _set_artist_status(db_conn, artist.id, MatchStatus.MANUAL_MATCHED)
        _insert_artist_match(db_conn, artist.id)

        # Mix of statuses on children, including manual decisions
        manual_matched_child = _insert_identity(
            db_conn, artist, original_title="Manual Matched Child",
            match_status=MatchStatus.MANUAL_MATCHED,
        )
        _insert_identity_match(db_conn, manual_matched_child.id)

        manual_rejected_child = _insert_identity(
            db_conn, artist, original_title="Manual Rejected Child",
            match_status=MatchStatus.MANUAL_REJECTED,
        )

        auto_matched_child = _insert_identity(
            db_conn, artist, original_title="Auto Matched Child",
            match_status=MatchStatus.AUTO_MATCHED,
        )
        _insert_identity_match(db_conn, auto_matched_child.id)

        pending_child = _insert_identity(
            db_conn, artist, original_title="Pending Child",
            match_status=MatchStatus.PENDING,
        )

        resp = client.post(f"/api/v1/matching/artists/{artist.id}/unmatch")
        assert resp.status_code == 200

        for child_id in (
            default_identity.id,
            manual_matched_child.id,
            manual_rejected_child.id,
            auto_matched_child.id,
            pending_child.id,
        ):
            row = db_conn.execute(
                "SELECT match_status, match_tier, reason_code "
                "FROM track_identities WHERE id = %s",
                (child_id,),
            ).fetchone()
            assert row is not None, f"identity {child_id} missing"
            assert row["match_status"] == "needs_review", (
                f"identity {child_id} not unmatched (status={row['match_status']})"
            )
            assert row["match_tier"] is None
            assert row["reason_code"] == "USER_UNMATCHED"

    def test_artist_unmatch_deletes_artist_and_child_matches(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        _set_artist_status(db_conn, artist.id, MatchStatus.AUTO_MATCHED)
        _set_identity_status(db_conn, identity.id, MatchStatus.AUTO_MATCHED)
        _insert_artist_match(db_conn, artist.id)
        _insert_identity_match(db_conn, identity.id)

        resp = client.post(f"/api/v1/matching/artists/{artist.id}/unmatch")
        assert resp.status_code == 200

        artist_match = db_conn.execute(
            "SELECT id FROM matches WHERE artist_id = %s", (artist.id,)
        ).fetchone()
        assert artist_match is None

        identity_match = db_conn.execute(
            "SELECT id FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert identity_match is None

    @pytest.mark.parametrize(
        "blocked_status",
        [MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW],
    )
    def test_unmatch_non_finalized_artist_returns_409(
        self, client, db_conn, blocked_status
    ):
        artist = _insert_artist(db_conn, match_status=blocked_status)

        resp = client.post(f"/api/v1/matching/artists/{artist.id}/unmatch")
        assert resp.status_code == 409

    def test_unmatch_unknown_artist_returns_404(self, client):
        resp = client.post(f"/api/v1/matching/artists/{uuid4()}/unmatch")
        assert resp.status_code == 404
