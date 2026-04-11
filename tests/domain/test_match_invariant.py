"""Test Match XOR invariant: exactly one of identity_id or artist_id must be set."""

from uuid import uuid4

import pytest

from backend.domain.enums import MatchTier
from backend.domain.matching import Match


class TestMatchXorInvariant:
    def test_neither_set_raises(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Match(
                id=uuid4(),
                confidence_score=0.9,
                match_tier=MatchTier.NORMALIZATION,
                identity_id=None,
                artist_id=None,
            )

    def test_both_set_raises(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Match(
                id=uuid4(),
                confidence_score=0.9,
                match_tier=MatchTier.NORMALIZATION,
                identity_id=uuid4(),
                artist_id=uuid4(),
            )

    def test_identity_only_valid(self) -> None:
        m = Match(
            id=uuid4(),
            confidence_score=0.9,
            match_tier=MatchTier.NORMALIZATION,
            identity_id=uuid4(),
            artist_id=None,
        )
        assert m.identity_id is not None
        assert m.artist_id is None

    def test_artist_only_valid(self) -> None:
        m = Match(
            id=uuid4(),
            confidence_score=0.9,
            match_tier=MatchTier.NORMALIZATION,
            identity_id=None,
            artist_id=uuid4(),
        )
        assert m.identity_id is None
        assert m.artist_id is not None
