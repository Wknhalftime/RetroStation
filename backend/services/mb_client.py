from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import structlog

from backend.domain.models import MbCache
from backend.repositories.mb_cache import MbCacheRepository

logger = structlog.get_logger()

_MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
_USER_AGENT = "RetroStation/0.1.0 (https://github.com/retrostation)"
_RATE_LIMIT_SECONDS = 1.1
_CACHE_TTL_DAYS = 30

_last_request_time: float = 0.0


def _rate_limit() -> None:
    """Enforce 1.1s between MusicBrainz API calls."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()


class RealMbClient:
    """MusicBrainz API client with caching and rate limiting."""

    def __init__(self, cache_repo: MbCacheRepository) -> None:
        self._cache = cache_repo
        self._http = httpx.Client(
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        cache_key = f"artist-search:{name.lower()}"

        # Check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cached.response_data.get("artists", [])  # type: ignore[no-any-return]

        # Rate limit and call API
        _rate_limit()
        response = self._http.get(
            f"{_MUSICBRAINZ_API}/artist/",
            params={"query": name, "fmt": "json", "limit": "10"},
        )
        response.raise_for_status()
        data = response.json()

        # Cache response
        now = datetime.now(tz=UTC)
        self._cache.set(MbCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="artist-search",
            entity_mbid="",
            response_data=data,
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        artists = data.get("artists", [])
        logger.info("mb_api_search", name=name, results=len(artists))
        return artists  # type: ignore[no-any-return]
