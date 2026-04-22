from __future__ import annotations

from backend.services.mb_client import MusicBrainzClientProtocol
from tests.fakes.mb_client import FakeMbClient


def test_protocol_has_search_recording() -> None:
    # Structural check: FakeMbClient must satisfy the Protocol for typing.
    fc: MusicBrainzClientProtocol = FakeMbClient()
    assert hasattr(fc, "search_recording")


def test_fake_search_recording_returns_seeded_results() -> None:
    fc = FakeMbClient(recording_searches={
        ("mbid-prince", "purple rain edit"): [
            {"id": "rec-1", "title": "Purple Rain (Edit)", "score": 97},
        ],
    })
    out = fc.search_recording(artist_mbid="mbid-prince", title="purple rain edit")
    assert len(out) == 1
    assert out[0]["id"] == "rec-1"
    assert out[0]["title"] == "Purple Rain (Edit)"


def test_fake_search_recording_returns_empty_for_unseeded() -> None:
    fc = FakeMbClient()
    assert fc.search_recording(artist_mbid="x", title="y") == []


def test_fake_search_recording_tracks_calls() -> None:
    fc = FakeMbClient()
    fc.search_recording(artist_mbid="mbid-x", title="title-y")
    assert "search_recording:mbid-x:title-y" in fc.calls
