from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus
from backend.services.matching_reasons import ReasonCode
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository


def _build_pending_artist() -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(),
        original_name="Prince",
        normalized_name="prince",
        match_status=MatchStatus.PENDING,
        created_at=datetime.now(UTC),
    )


def _seed(repo: FakeBroadcastArtistRepository) -> BroadcastArtist:
    a = _build_pending_artist()
    repo.upsert(a)
    return a


def test_update_match_status_accepts_reason_code_and_detail() -> None:
    repo = FakeBroadcastArtistRepository()
    artist = _seed(repo)
    repo.update_match_status(
        artist.id,
        MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.LOW_CONFIDENCE,
        reason_detail="Score 65% — below confidence threshold",
    )
    stored = repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert repo._reason_codes[artist.id] == ReasonCode.LOW_CONFIDENCE
    assert repo._reason_details[artist.id] == "Score 65% — below confidence threshold"


def test_update_match_status_reason_args_default_to_none() -> None:
    repo = FakeBroadcastArtistRepository()
    artist = _seed(repo)
    repo.update_match_status(artist.id, MatchStatus.NEEDS_REVIEW)
    # Default to None — same as if the keys don't exist
    assert repo._reason_codes.get(artist.id) is None
    assert repo._reason_details.get(artist.id) is None


def test_update_match_status_clears_previous_reason_when_none_passed() -> None:
    """A transition back to AUTO_MATCHED or PENDING should clear stale reason.
    Calling with reason_code=None explicitly overwrites, matching Pg UPDATE."""
    repo = FakeBroadcastArtistRepository()
    artist = _seed(repo)
    repo.update_match_status(
        artist.id,
        MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.LOW_CONFIDENCE,
        reason_detail="Score 65% — below confidence threshold",
    )
    repo.update_match_status(artist.id, MatchStatus.AUTO_MATCHED)  # clears
    assert repo._reason_codes[artist.id] is None
    assert repo._reason_details[artist.id] is None
