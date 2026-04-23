from __future__ import annotations

from typing import Any


class FakeMbClient:
    """In-memory MusicBrainz client for testing. Returns canned responses."""

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        releases: dict[str, dict[str, Any]] | None = None,
        recordings: dict[str, dict[str, Any]] | None = None,
        recording_searches: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._releases = releases or {}
        self._recordings = recordings or {}
        self._recording_searches = recording_searches or {}
        self.calls: list[str] = []

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(name)
        return self._responses.get(name, [])

    def lookup_release(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_release:{mbid}")
        return self._releases.get(mbid)

    def lookup_recording(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_recording:{mbid}")
        return self._recordings.get(mbid)

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        self.calls.append(f"search_recording:{artist_mbid}:{title}")
        return self._recording_searches.get((artist_mbid, title), [])
