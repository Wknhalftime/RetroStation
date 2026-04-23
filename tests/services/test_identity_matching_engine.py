"""Unit tests for IdentityMatchingEngine.

The engine walks strategies in order and returns the first non-None result.
Strategies produce values only — persistence lives in the service function.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier
from backend.services.identity_matching_service import (
    IdentityMatchingEngine,
    IdentityMatchResult,
)


def _identity() -> BroadcastTrackIdentity:
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=uuid4(),
        original_title="X",
        normalized_title="x",
        normalized_signature="a|x",
    )


def _artist() -> BroadcastArtist:
    return BroadcastArtist(id=uuid4(), original_name="A", normalized_name="a")


class _StubStrategy:
    def __init__(self, result: IdentityMatchResult | None) -> None:
        self._result = result
        self.calls = 0

    def apply(
        self,
        identity: BroadcastTrackIdentity,
        artist: BroadcastArtist,
    ) -> IdentityMatchResult | None:
        self.calls += 1
        return self._result


def test_engine_first_strategy_wins_short_circuits() -> None:
    expected = IdentityMatchResult(
        status=MatchStatus.AUTO_MATCHED,
        tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        confidence_score=100.0,
        library_file_id=uuid4(),
    )
    first = _StubStrategy(expected)
    second = _StubStrategy(None)

    engine = IdentityMatchingEngine([first, second])
    assert engine.resolve(_identity(), _artist()) is expected
    assert first.calls == 1
    assert second.calls == 0


def test_engine_falls_through_to_next_when_first_returns_none() -> None:
    expected = IdentityMatchResult(
        status=MatchStatus.NEEDS_REVIEW,
        tier=MatchTier.LOCAL_FILE_FUZZY,
        confidence_score=55.0,
        library_file_id=uuid4(),
    )
    first = _StubStrategy(None)
    second = _StubStrategy(expected)
    third = _StubStrategy(None)

    engine = IdentityMatchingEngine([first, second, third])
    assert engine.resolve(_identity(), _artist()) is expected
    assert first.calls == 1
    assert second.calls == 1
    assert third.calls == 0


def test_engine_all_none_returns_none() -> None:
    first = _StubStrategy(None)
    second = _StubStrategy(None)

    engine = IdentityMatchingEngine([first, second])
    assert engine.resolve(_identity(), _artist()) is None
    assert first.calls == 1
    assert second.calls == 1


def test_engine_empty_strategy_list_returns_none() -> None:
    engine: IdentityMatchingEngine = IdentityMatchingEngine([])
    assert engine.resolve(_identity(), _artist()) is None


def test_engine_preserves_strategy_order() -> None:
    order: list[str] = []

    class _Recorder:
        def __init__(self, name: str, result: IdentityMatchResult | None) -> None:
            self._name = name
            self._result = result

        def apply(self, i: Any, a: Any) -> IdentityMatchResult | None:
            order.append(self._name)
            return self._result

    engine = IdentityMatchingEngine([
        _Recorder("a", None),
        _Recorder("b", None),
        _Recorder("c", None),
    ])
    engine.resolve(_identity(), _artist())
    assert order == ["a", "b", "c"]
