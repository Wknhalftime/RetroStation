from abc import ABC, abstractmethod
from collections.abc import Sequence

from backend.domain.system import MusicBrainzCache


class MusicBrainzCacheRepository(ABC):
    @abstractmethod
    def get(self, cache_key: str) -> MusicBrainzCache | None: ...

    @abstractmethod
    def get_many(self, cache_keys: Sequence[str]) -> dict[str, MusicBrainzCache]:
        """Unexpired entries for *cache_keys*, keyed by cache key; misses are absent."""
        ...

    @abstractmethod
    def set(self, cache: MusicBrainzCache) -> None: ...

    @abstractmethod
    def set_many(self, caches: Sequence[MusicBrainzCache]) -> None:
        """Insert or overwrite every entry, one round trip."""
        ...

    @abstractmethod
    def delete_expired(self) -> int:
        """Delete all rows where expires_at < now(). Returns count deleted."""
        ...

