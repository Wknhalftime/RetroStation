from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import structlog

from backend.domain.system import MusicBrainzCache
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository

logger = structlog.get_logger()

_MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
_USER_AGENT = "RetroStation/0.1.0 (https://github.com/retrostation)"
_RATE_LIMIT_SECONDS = 1.1
_CACHE_TTL_DAYS = 30

_rate_lock: threading.Lock = threading.Lock()
_last_request_time: float = 0.0


def _rate_limit() -> None:
    """Enforce 1.1s between MusicBrainz API calls (thread-safe)."""
    global _last_request_time
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _RATE_LIMIT_SECONDS:
            time.sleep(_RATE_LIMIT_SECONDS - elapsed)
        _last_request_time = time.monotonic()


class RealMbClient:
    """MusicBrainz API client with caching and rate limiting."""

    def __init__(self, cache_repo: MusicBrainzCacheRepository) -> None:
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
        self._cache.set(MusicBrainzCache(
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

    def lookup_release(self, mbid: str) -> dict[str, Any] | None:
        """Fetch a release by MBID, including recordings, artist-credits, and release-groups."""
        cache_key = f"release:{mbid}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cached.response_data

        _rate_limit()
        response = self._http.get(
            f"{_MUSICBRAINZ_API}/release/{mbid}",
            params={"fmt": "json", "inc": "recordings+artist-credits+release-groups"},
        )
        if response.status_code == 404:
            logger.info("mb_release_not_found", mbid=mbid)
            return None
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="release",
            entity_mbid=mbid,
            response_data=data,
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup_release", mbid=mbid)
        return data

    def lookup_recording(self, mbid: str) -> dict[str, Any] | None:
        """Fetch a recording by MBID, including artist-credits and work relations."""
        cache_key = f"recording:{mbid}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cached.response_data

        _rate_limit()
        response = self._http.get(
            f"{_MUSICBRAINZ_API}/recording/{mbid}",
            params={"fmt": "json", "inc": "artist-credits+work-rels"},
        )
        if response.status_code == 404:
            logger.info("mb_recording_not_found", mbid=mbid)
            return None
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="recording",
            entity_mbid=mbid,
            response_data=data,
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup_recording", mbid=mbid)
        return data
