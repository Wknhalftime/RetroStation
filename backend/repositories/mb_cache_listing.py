"""What ``mb_cache`` holds, expired or not (phase 2a S4, S10).

The cache's own port answers one live key at a time. A backfill reads what MusicBrainz once said,
however old, so it asks here and no request goes out.
"""

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping


class MusicBrainzCacheListing(ABC):
    @abstractmethod
    def rows(self, cache_keys: Collection[str]) -> dict[str, Mapping[str, object]]:
        """Each of these keys the cache holds, with its response."""
        ...

    @abstractmethod
    def rows_with_prefix(self, prefix: str) -> list[tuple[str, Mapping[str, object]]]:
        """Every row whose key starts with *prefix*, taken literally, in key order."""
        ...
