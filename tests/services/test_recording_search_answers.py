"""RecordingSearch's remembered answers, which trial mode uses (AUD-R100, SPEC S9).

Both of a song's scoring chains search MusicBrainz with the same artist MBID and title, so in
trial mode the second uses the first's answer. A remembered answer counts as a successful
search for the outage cut-off (AUD-R028), as the client's own cache hit would; a failure is
never remembered.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.domain.matching import MusicBrainzUnavailableError
from backend.services.recording_search import RecordingSearch
from tests.fakes.mb_client import FakeMbClient

_FOUND = [{"id": "rec-1"}]


class _FlakyMb(FakeMbClient):
    """Answers ("mbid", "found") and fails every other search with a dropped connection."""

    def __init__(self) -> None:
        super().__init__(recording_searches={("mbid", "found"): _FOUND})

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        found = super().search_recording(artist_mbid, title, limit)
        if title != "found":
            raise httpx.ConnectError("connection refused")
        return found

    @property
    def searches(self) -> int:
        return sum(1 for call in self.calls if call.startswith("search_recording:"))


def test_a_repeated_search_calls_musicbrainz_once() -> None:
    mb = _FlakyMb()
    search = RecordingSearch(mb, remember_answers=True)

    assert search.search("mbid", "found") == _FOUND
    assert search.search("mbid", "found") == _FOUND
    assert mb.searches == 1


def test_without_remembering_every_search_calls_musicbrainz() -> None:
    mb = _FlakyMb()
    search = RecordingSearch(mb)

    search.search("mbid", "found")
    search.search("mbid", "found")

    assert mb.searches == 2


def test_a_failure_is_not_remembered() -> None:
    mb = _FlakyMb()
    search = RecordingSearch(mb, remember_answers=True)

    for _ in range(2):
        with pytest.raises(MusicBrainzUnavailableError):
            search.search("mbid", "lost")

    assert mb.searches == 2


def test_a_remembered_answer_resets_the_cut_off_count() -> None:
    mb = _FlakyMb()
    search = RecordingSearch(mb, cutoff=2, remember_answers=True)
    search.search("mbid", "found")
    with pytest.raises(MusicBrainzUnavailableError):
        search.search("mbid", "lost")

    search.search("mbid", "found")  # remembered: no call, but a success
    with pytest.raises(MusicBrainzUnavailableError):
        search.search("mbid", "lost")

    assert not search.cut_off
