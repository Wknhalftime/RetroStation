from __future__ import annotations

from typing import Any

import httpx


class FakeMbClient:
    """In-memory MusicBrainz client for testing. Returns canned responses.

    `error_mbids` makes `lookup_artist` / `lookup_recording` raise
    `httpx.ConnectError` for selected MBIDs — useful for exercising the
    coalescing helpers' "omit on transient error" paths without hand-rolling
    an inline test double.

    `bad_request_mbids` makes the three lookups raise the `HTTPStatusError`
    (400 "Invalid mbid.") the real client raises for a malformed MBID — see
    the recorded evidence in tests/services/test_mb_client_cassettes.py.
    """

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        releases: dict[str, dict[str, Any]] | None = None,
        recordings: dict[str, dict[str, Any]] | None = None,
        recording_searches: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        artists: dict[str, dict[str, Any]] | None = None,
        error_mbids: set[str] | None = None,
        bad_request_mbids: set[str] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._releases = releases or {}
        self._recordings = recordings or {}
        self._recording_searches = recording_searches or {}
        self._artists = artists or {}
        self._error_mbids = error_mbids or set()
        self._bad_request_mbids = bad_request_mbids or set()
        self.calls: list[str] = []
        # Observability counters exposed by MusicBrainzClientProtocol. Tests
        # that care about counter deltas can mutate these directly; tests
        # that don't care leave them at 0.
        self.live_fetches: int = 0
        self.cache_hits: int = 0

    def _raise_if_bad_request(self, entity: str, mbid: str) -> None:
        if mbid not in self._bad_request_mbids:
            return
        request = httpx.Request("GET", f"https://musicbrainz.org/ws/2/{entity}/{mbid}")
        response = httpx.Response(400, json={"error": "Invalid mbid."}, request=request)
        raise httpx.HTTPStatusError(
            "Client error '400 Bad Request'", request=request, response=response
        )

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(name)
        return self._responses.get(name, [])

    def lookup_release(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_release:{mbid}")
        self._raise_if_bad_request("release", mbid)
        return self._releases.get(mbid)

    def lookup_recording(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_recording:{mbid}")
        self._raise_if_bad_request("recording", mbid)
        if mbid in self._error_mbids:
            raise httpx.ConnectError("simulated transient failure")
        return self._recordings.get(mbid)

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        self.calls.append(f"search_recording:{artist_mbid}:{title}")
        return self._recording_searches.get((artist_mbid, title), [])

    def lookup_artist(self, mbid: str) -> dict[str, Any] | None:
        self.calls.append(f"lookup_artist:{mbid}")
        self._raise_if_bad_request("artist", mbid)
        if mbid in self._error_mbids:
            raise httpx.ConnectError("simulated transient failure")
        return self._artists.get(mbid)
