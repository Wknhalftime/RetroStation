"""In-memory fake of the backfill's read of ``mb_cache`` (artist model phase 2a, SPEC S4, S10)."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from backend.repositories.mb_cache_listing import MusicBrainzCacheListing


class FakeMusicBrainzCacheListing(MusicBrainzCacheListing):
    def __init__(self) -> None:
        self.payloads: dict[str, Mapping[str, object]] = {}

    def rows(self, cache_keys: Collection[str]) -> dict[str, Mapping[str, object]]:
        return {key: self.payloads[key] for key in cache_keys if key in self.payloads}

    def rows_with_prefix(self, prefix: str) -> list[tuple[str, Mapping[str, object]]]:
        return sorted(
            (key, payload) for key, payload in self.payloads.items() if key.startswith(prefix)
        )
