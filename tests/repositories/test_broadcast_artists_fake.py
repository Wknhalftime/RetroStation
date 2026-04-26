from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, ReasonCode
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


def _seed_named(repo: FakeBroadcastArtistRepository, name: str) -> BroadcastArtist:
    a = BroadcastArtist(
        id=uuid4(),
        original_name=name,
        normalized_name=name.lower().strip(),
    )
    return repo.upsert(a)


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
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert stored.reason_detail == "Score 65% — below confidence threshold"


def test_update_match_status_reason_args_default_to_none() -> None:
    repo = FakeBroadcastArtistRepository()
    artist = _seed(repo)
    repo.update_match_status(artist.id, MatchStatus.NEEDS_REVIEW)
    stored = repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.reason_code is None
    assert stored.reason_detail is None


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
    stored = repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.reason_code is None
    assert stored.reason_detail is None


def test_get_by_ids_returns_requested_subset() -> None:
    repo = FakeBroadcastArtistRepository()
    a1 = _seed_named(repo, "Prince")
    _seed_named(repo, "Madonna")
    a3 = _seed_named(repo, "Bowie")
    out = repo.get_by_ids([a1.id, a3.id])
    assert {a.id for a in out} == {a1.id, a3.id}


def test_get_by_ids_empty_input_returns_empty_list() -> None:
    repo = FakeBroadcastArtistRepository()
    assert repo.get_by_ids([]) == []


def test_get_by_ids_missing_ids_silently_omitted() -> None:
    repo = FakeBroadcastArtistRepository()
    a1 = _seed_named(repo, "Prince")
    out = repo.get_by_ids([a1.id, uuid4()])
    assert len(out) == 1 and out[0].id == a1.id
