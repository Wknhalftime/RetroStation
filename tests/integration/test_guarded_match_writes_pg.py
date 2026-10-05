"""AUD-R018 G1 against PostgreSQL: `update_match_status_if_pending` writes only a PENDING row.

The service-level race tests (tests/services/test_guarded_worker_writes.py) run on the fakes;
this file pins that the Pg repositories honour the same contract. The two-connection test shows
a decision committed by another connection (the API process) between the worker's read and its
write survives, because the guard is a single UPDATE ... WHERE match_status = 'pending'.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode


def _unique(name: str) -> str:
    """Unique names keep each test's rows distinct from any it creates itself."""
    return f"{name}-{uuid4().hex}"


def _artist_row(conn: psycopg.Connection[dict[str, object]], row_id: object) -> dict[str, object]:
    row = conn.execute(
        "SELECT match_status, reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
        (row_id,),
    ).fetchone()
    assert row is not None
    return row


def test_artist_guarded_write_applies_to_a_pending_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        artist = repo.upsert(
            BroadcastArtist(id=uuid4(), original_name="ABBA", normalized_name=_unique("abba"))
        )

        wrote = repo.update_match_status_if_pending(
            artist.id,
            MatchStatus.NEEDS_REVIEW,
            reason_code=ReasonCode.DEFERRED_RETRY,
            reason_detail="retry later",
        )

        assert wrote is True
        row = _artist_row(conn, artist.id)
        assert row["match_status"] == MatchStatus.NEEDS_REVIEW.value
        assert row["reason_code"] == ReasonCode.DEFERRED_RETRY.value
        assert row["reason_detail"] == "retry later"


@pytest.mark.parametrize("decided", [MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED])
def test_artist_guarded_write_leaves_a_decided_row_untouched(
    migrated_db: str, decided: MatchStatus
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        artist = repo.upsert(
            BroadcastArtist(id=uuid4(), original_name="BLONDIE", normalized_name=_unique("blondie"))
        )
        repo.update_match_status(artist.id, decided, reason_detail="kept")

        wrote = repo.update_match_status_if_pending(
            artist.id, MatchStatus.AUTO_REJECTED, reason_code=ReasonCode.NO_CANDIDATES
        )

        assert wrote is False
        row = _artist_row(conn, artist.id)
        assert row["match_status"] == decided.value
        assert row["reason_code"] is None
        assert row["reason_detail"] == "kept"


def _identity(conn: psycopg.Connection[dict[str, object]]) -> BroadcastTrackIdentity:
    artist = PgBroadcastArtistRepository(conn).upsert(
        BroadcastArtist(id=uuid4(), original_name="CHIC", normalized_name=_unique("chic"))
    )
    return PgBroadcastTrackIdentityRepository(conn).upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Le Freak",
            normalized_title="le freak",
            normalized_signature=_unique("chic00lefreak"),
        )
    )


def test_identity_guarded_write_applies_to_a_pending_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastTrackIdentityRepository(conn)
        identity = _identity(conn)

        wrote = repo.update_match_status_if_pending(
            identity.id, MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION
        )

        assert wrote is True
        stored = repo.get_by_id(identity.id)
        assert stored is not None
        assert stored.match_status == MatchStatus.AUTO_MATCHED
        assert stored.match_tier == MatchTier.NORMALIZATION


@pytest.mark.parametrize(
    "decided",
    [MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED, MatchStatus.AUTO_REJECTED],
)
def test_identity_guarded_write_leaves_a_decided_row_untouched(
    migrated_db: str, decided: MatchStatus
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastTrackIdentityRepository(conn)
        identity = _identity(conn)
        repo.update_match_status(identity.id, decided, MatchTier.MANUAL)

        wrote = repo.update_match_status_if_pending(
            identity.id, MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION
        )

        assert wrote is False
        stored = repo.get_by_id(identity.id)
        assert stored is not None
        assert stored.match_status == decided
        assert stored.match_tier == MatchTier.MANUAL


def test_a_decision_committed_by_another_connection_wins(migrated_db: str) -> None:
    with (
        psycopg.connect(migrated_db, row_factory=dict_row) as worker,
        psycopg.connect(migrated_db, row_factory=dict_row) as api,
    ):
        worker_repo = PgBroadcastArtistRepository(worker)
        artist = worker_repo.upsert(
            BroadcastArtist(id=uuid4(), original_name="DEVO", normalized_name=_unique("devo"))
        )
        worker.commit()
        # The worker reads the row as PENDING, then the API commits a decision.
        pending = worker_repo.get_by_id(artist.id)
        assert pending is not None
        assert pending.match_status == MatchStatus.PENDING
        PgBroadcastArtistRepository(api).update_match_status(artist.id, MatchStatus.MANUAL_MATCHED)
        api.commit()

        wrote = worker_repo.update_match_status_if_pending(artist.id, MatchStatus.AUTO_REJECTED)
        worker.commit()

        assert wrote is False
        row = _artist_row(worker, artist.id)
        assert row["match_status"] == MatchStatus.MANUAL_MATCHED.value
