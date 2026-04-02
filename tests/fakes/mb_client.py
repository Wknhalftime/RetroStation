from __future__ import annotations

from typing import Any


class FakeMbClient:
    """In-memory MusicBrainz client for testing. Returns canned responses."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(name)
        return self._responses.get(name, [])
