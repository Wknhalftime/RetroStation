"""Unit tests for ArtistMatchingEngine.

The engine walks strategies in order and returns the first non-None result.
Strategies produce values only — persistence lives in the service function
(match_artists_for_playlist).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, MatchTier
from backend.services.artist_matching_service import (
    ArtistMatchingEngine,
    ArtistMatchResult,
)


def _artist() -> BroadcastArtist:
    return BroadcastArtist(id=uuid4(), original_name="A", normalized_name="a")


class _StubStrategy:
    def __init__(self, result: ArtistMatchResult | None) -> None:
        self._result = result
        self.calls = 0

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        self.calls += 1
        return self._result


def test_engine_first_strategy_wins_short_circuits() -> None:
    expected = ArtistMatchResult(
        status=MatchStatus.AUTO_MATCHED,
        tier=MatchTier.MANUAL,
        confidence_score=100.0,
        target_id="mbid-x",
    )
    first = _StubStrategy(expected)
    second = _StubStrategy(None)

    engine = ArtistMatchingEngine([first, second])
    assert engine.resolve(_artist()) is expected
    assert first.calls == 1
    assert second.calls == 0


def test_engine_falls_through_to_next_when_first_returns_none() -> None:
    expected = ArtistMatchResult(
        status=MatchStatus.AUTO_MATCHED,
        tier=MatchTier.NORMALIZATION,
        confidence_score=100.0,
        target_id="mbid-y",
    )
    first = _StubStrategy(None)
    second = _StubStrategy(expected)
    third = _StubStrategy(None)

    engine = ArtistMatchingEngine([first, second, third])
    assert engine.resolve(_artist()) is expected
    assert first.calls == 1
    assert second.calls == 1
    assert third.calls == 0


def test_engine_all_none_returns_none() -> None:
    first = _StubStrategy(None)
    second = _StubStrategy(None)

    engine = ArtistMatchingEngine([first, second])
    assert engine.resolve(_artist()) is None
    assert first.calls == 1
    assert second.calls == 1


def test_engine_empty_strategy_list_returns_none() -> None:
    engine: ArtistMatchingEngine = ArtistMatchingEngine([])
    assert engine.resolve(_artist()) is None


def test_engine_preserves_strategy_order() -> None:
    order: list[str] = []

    class _Recorder:
        def __init__(self, name: str, result: ArtistMatchResult | None) -> None:
            self._name = name
            self._result = result

        def apply(self, a: Any) -> ArtistMatchResult | None:
            order.append(self._name)
            return self._result

    engine = ArtistMatchingEngine([
        _Recorder("a", None),
        _Recorder("b", None),
        _Recorder("c", None),
    ])
    engine.resolve(_artist())
    assert order == ["a", "b", "c"]
