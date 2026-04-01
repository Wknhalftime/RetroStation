from abc import ABC, abstractmethod

from backend.domain.models import MbCache


class MbCacheRepository(ABC):
    @abstractmethod
    def get(self, cache_key: str) -> MbCache | None: ...

    @abstractmethod
    def set(self, cache: MbCache) -> None: ...

    @abstractmethod
    def delete_expired(self) -> int:
        """Delete all rows where expires_at < now(). Returns count deleted."""
        ...
