from abc import ABC, abstractmethod

from backend.domain.models import MusicBrainzCache


class MusicBrainzCacheRepository(ABC):
    @abstractmethod
    def get(self, cache_key: str) -> MusicBrainzCache | None: ...

    @abstractmethod
    def set(self, cache: MusicBrainzCache) -> None: ...

    @abstractmethod
    def delete_expired(self) -> int:
        """Delete all rows where expires_at < now(). Returns count deleted."""
        ...
