from datetime import UTC, datetime

from backend.domain.models import MbCache
from backend.repositories.mb_cache import MbCacheRepository


class FakeMbCacheRepository(MbCacheRepository):
    def __init__(self) -> None:
        self._data: dict[str, MbCache] = {}

    def get(self, cache_key: str) -> MbCache | None:
        entry = self._data.get(cache_key)
        if entry and entry.expires_at > datetime.now(tz=UTC):
            return entry
        return None

    def set(self, cache: MbCache) -> None:
        self._data[cache.cache_key] = cache

    def delete_expired(self) -> int:
        now = datetime.now(tz=UTC)
        expired = [k for k, v in self._data.items() if v.expires_at <= now]
        for k in expired:
            del self._data[k]
        return len(expired)
