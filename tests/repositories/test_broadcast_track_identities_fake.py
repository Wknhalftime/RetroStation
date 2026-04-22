from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier
from backend.services.matching_reasons import ReasonCode
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository


def _build_pending_identity() -> BroadcastTrackIdentity:
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=uuid4(),
        original_title="Purple Rain",
        normalized_title="purple rain",
        normalized_signature="prince::purple rain",
        match_status=MatchStatus.PENDING,
        created_at=datetime.now(UTC),
    )


def _seed(repo: FakeBroadcastTrackIdentityRepository) -> BroadcastTrackIdentity:
    identity = _build_pending_identity()
    repo.upsert(identity)
    return identity


def test_update_match_status_accepts_reason_code_and_detail() -> None:
    repo = FakeBroadcastTrackIdentityRepository()
    identity = _seed(repo)
    repo.update_match_status(
        identity.id,
        MatchStatus.NEEDS_REVIEW,
        tier=None,
        reason_code=ReasonCode.NO_CANDIDATES,
        reason_detail="No library files found for artist",
    )
    assert repo._reason_codes[identity.id] == ReasonCode.NO_CANDIDATES
    assert repo._reason_details[identity.id] == "No library files found for artist"


def test_update_match_status_reason_args_default_to_none() -> None:
    repo = FakeBroadcastTrackIdentityRepository()
    identity = _seed(repo)
    repo.update_match_status(identity.id, MatchStatus.NEEDS_REVIEW, tier=None)
    assert repo._reason_codes.get(identity.id) is None
    assert repo._reason_details.get(identity.id) is None


def test_update_match_status_clears_previous_reason_when_none_passed() -> None:
    repo = FakeBroadcastTrackIdentityRepository()
    identity = _seed(repo)
    repo.update_match_status(
        identity.id,
        MatchStatus.NEEDS_REVIEW,
        tier=None,
        reason_code=ReasonCode.NO_CANDIDATES,
        reason_detail="No library files found for artist",
    )
    repo.update_match_status(
        identity.id, MatchStatus.AUTO_MATCHED, tier=MatchTier.MUSICBRAINZ_ID_EXACT
    )
    assert repo._reason_codes[identity.id] is None
    assert repo._reason_details[identity.id] is None
