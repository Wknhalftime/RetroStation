from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.services.identity_matching_service import (
    IdentityMatchingStrategy,
    IdentityMatchResult,
)


def test_identity_match_result_is_frozen() -> None:
    r = IdentityMatchResult(
        status=MatchStatus.NEEDS_REVIEW,
        tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
        confidence_score=73.0,
        library_file_id=uuid4(),
        reason_code=ReasonCode.LOW_CONFIDENCE,
        reason_detail="Score 73% — below confidence threshold",
    )
    with pytest.raises(FrozenInstanceError):
        r.status = MatchStatus.AUTO_MATCHED  # type: ignore[misc]


def test_identity_match_result_allows_none_library_file_id() -> None:
    r = IdentityMatchResult(
        status=MatchStatus.NEEDS_REVIEW,
        tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
        confidence_score=0.0,
        library_file_id=None,
        reason_code=ReasonCode.NO_LOCAL_FILES,
        reason_detail="Artist MBID confirmed but no matching local recording found",
    )
    assert r.library_file_id is None


def test_identity_match_result_auto_matched_has_optional_reason() -> None:
    r = IdentityMatchResult(
        status=MatchStatus.AUTO_MATCHED,
        tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        confidence_score=100.0,
        library_file_id=uuid4(),
    )
    assert r.reason_code is None
    assert r.reason_detail is None


def test_identity_matching_strategy_protocol_is_structurally_checkable() -> None:
    class _Stub:
        def apply(self, identity, artist):  # type: ignore[no-untyped-def]
            return None

    strategy: IdentityMatchingStrategy = _Stub()
    assert hasattr(strategy, "apply")
