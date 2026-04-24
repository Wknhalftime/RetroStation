from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import uuid4

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.domain.system import MusicBrainzCache
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository
from backend.services.mb_types import MbArtist, MbArtistResult, MbRecording, MbRelease

logger = structlog.get_logger()

# --- Module-level constants ---
_MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
_USER_AGENT = "RetroStation/0.1.0 (https://github.com/retrostation)"
_RATE_LIMIT_SECONDS = 1.1
_CACHE_TTL_DAYS = 30

# --- Module-level mutable state (rate-limiting) ---
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


# Lucene special characters that must be escaped inside a query term.
# See https://musicbrainz.org/doc/MusicBrainz_API/Search#Lucene_Search_Syntax.
_LUCENE_SPECIALS = r'+-&|!(){}[]^"~*?:\/'


def _lucene_escape(value: str) -> str:
    """Escape Lucene special characters so a user string is a literal term."""
    return "".join(("\\" + ch) if ch in _LUCENE_SPECIALS else ch for ch in value)


def _is_transient_mb_error(exc: BaseException) -> bool:
    """Return True for HTTP errors that are transient and worth retrying (429, 5xx)."""
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or exc.response.status_code >= 500)
    )


class MusicBrainzClientProtocol(Protocol):
    """Protocol for MusicBrainz API clients (production + test doubles)."""

    def search_artist(self, name: str) -> list[MbArtistResult]: ...
    def lookup_artist(self, mbid: str) -> MbArtist | None: ...
    def lookup_release(self, mbid: str) -> MbRelease | None: ...
    def lookup_recording(self, mbid: str) -> MbRecording | None: ...
    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[MbRecording]: ...


class MusicBrainzApiClient:
    """MusicBrainz API client with caching, rate limiting, and exponential-backoff retry.

    Threading contract
    ------------------
    This client is designed for **cross-thread sequential** use, not concurrent
    use. It is safe for the instance (and its backing resources) to be touched
    from different threads as long as at most one thread uses it at a time.

    The contract is:

    - Calls from the Huey sync worker (single thread).
    - Calls from FastAPI sync generator dependencies whose setup, yielded body,
      and teardown may each land on a different anyio threadpool worker, and
      whose handlers may further dispatch the call through ``asyncio.to_thread``.

    This holds today because:

    - ``psycopg`` (v3) sync ``Connection`` objects have no thread-local state
      and their operations are protected by an internal lock, so serial use
      across threads is supported.
    - ``httpx.Client`` is documented as safe to share across threads.

    If a future change swaps either backing resource for a driver that enforces
    a creator-thread check or relies on thread-local state (stdlib ``sqlite3``
    with ``check_same_thread=True``, some ODBC drivers, etc.), the call sites
    in ``backend/dependencies.py::get_mb_client`` and
    ``backend/tasks/artist_matching_tasks.py`` must be revisited — either by
    pinning the client to a single thread, or by constructing a fresh client
    per thread.
    """

    def __init__(self, cache_repo: MusicBrainzCacheRepository) -> None:
        self._cache = cache_repo
        self._http = httpx.Client(
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )

    def __enter__(self) -> MusicBrainzApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    @retry(
        retry=retry_if_exception(_is_transient_mb_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _fetch(self, url: str, params: dict[str, str]) -> httpx.Response:
        """Rate-limited HTTP GET; raises HTTPStatusError on any non-2xx response.

        Decorated with tenacity retry: up to 3 attempts with exponential backoff
        (2–30s) on transient errors (429, 5xx).  Non-transient errors (e.g. 404)
        are re-raised immediately without retrying.
        """
        _rate_limit()
        response = self._http.get(url, params=params)
        response.raise_for_status()
        return response

    def search_artist(self, name: str) -> list[MbArtistResult]:
        """Search MusicBrainz for artists matching name.

        Results are cached in the local MusicBrainz cache as a transparent
        read-through side-effect. This is intentional (cache-aside pattern).
        """
        cache_key = f"artist-search:{name.lower()}"

        cached_entry = self._cache.get(cache_key)
        if cached_entry is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cast(list[MbArtistResult], cached_entry.response_data.get("artists", []))

        response = self._fetch(
            f"{_MUSICBRAINZ_API}/artist/",
            {"query": name, "fmt": "json", "limit": "10"},
        )
        response_payload: dict[str, object] = response.json()

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="artist-search",
            entity_mbid="",
            response_data=dict(response_payload),
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        artists = cast(list[MbArtistResult], response_payload.get("artists", []))
        logger.info("mb_api_search", name=name, results=len(artists))
        return artists

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[MbRecording]:
        """Search MusicBrainz for recordings by artist MBID and title.

        Results are cached in the local MusicBrainz cache as a transparent
        read-through side-effect. This is intentional (cache-aside pattern).
        """
        # `limit` is part of the cache key so a later call asking for more
        # results doesn't reuse a truncated response from an earlier call.
        cache_key = f"recording-search:{artist_mbid}:{title.lower()}:{limit}"

        cached_entry = self._cache.get(cache_key)
        if cached_entry is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cast(
                list[MbRecording], cached_entry.response_data.get("recordings", [])
            )

        response = self._fetch(
            f"{_MUSICBRAINZ_API}/recording/",
            {
                "query": (
                    f"arid:{_lucene_escape(artist_mbid)} "
                    f'AND recording:"{_lucene_escape(title)}"'
                ),
                "fmt": "json",
                "limit": str(limit),
            },
        )
        response_payload: dict[str, object] = response.json()

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="recording-search",
            entity_mbid="",
            response_data=dict(response_payload),
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        recordings = cast(list[MbRecording], response_payload.get("recordings", []))
        logger.info(
            "mb_api_search_recording",
            artist_mbid=artist_mbid,
            title=title,
            results=len(recordings),
        )
        return recordings

    def lookup_release(self, mbid: str) -> MbRelease | None:
        """Fetch a release by MBID, including recordings, artist-credits, and release-groups.

        Results are cached in the local MusicBrainz cache as a transparent
        read-through side-effect. This is intentional (cache-aside pattern).
        """
        cache_key = f"release:{mbid}"

        cached_entry = self._cache.get(cache_key)
        if cached_entry is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cast(MbRelease, cached_entry.response_data)

        try:
            response = self._fetch(
                f"{_MUSICBRAINZ_API}/release/{mbid}",
                {"fmt": "json", "inc": "recordings+artist-credits+release-groups"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.info("mb_release_not_found", mbid=mbid)
                return None
            raise

        release = cast(MbRelease, response.json())

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="release",
            entity_mbid=mbid,
            response_data=dict(release),
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup_release", mbid=mbid)
        return release

    def lookup_recording(self, mbid: str) -> MbRecording | None:
        """Fetch a recording by MBID, including artist-credits and work relations.

        Results are cached in the local MusicBrainz cache as a transparent
        read-through side-effect. This is intentional (cache-aside pattern).
        """
        cache_key = f"recording:{mbid}"

        cached_entry = self._cache.get(cache_key)
        if cached_entry is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cast(MbRecording, cached_entry.response_data)

        try:
            response = self._fetch(
                f"{_MUSICBRAINZ_API}/recording/{mbid}",
                {"fmt": "json", "inc": "artist-credits+work-rels"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.info("mb_recording_not_found", mbid=mbid)
                return None
            raise

        recording = cast(MbRecording, response.json())

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="recording",
            entity_mbid=mbid,
            response_data=dict(recording),
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup_recording", mbid=mbid)
        return recording

    def lookup_artist(self, mbid: str) -> MbArtist | None:
        """Fetch an artist by MBID, including aliases and tags.

        Cache-aside read-through — identical pattern to lookup_recording.
        `url-rels` is intentionally omitted: MbArtist does not surface it and no
        downstream code reads it, so including it would bloat cache rows for no
        benefit.
        """
        cache_key = f"artist:{mbid}"

        cached_entry = self._cache.get(cache_key)
        if cached_entry is not None:
            logger.debug("mb_cache_hit", cache_key=cache_key)
            return cast(MbArtist, cached_entry.response_data)

        try:
            response = self._fetch(
                f"{_MUSICBRAINZ_API}/artist/{mbid}",
                {"fmt": "json", "inc": "aliases+tags"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.info("mb_artist_not_found", mbid=mbid)
                return None
            raise

        artist = cast(MbArtist, response.json())

        now = datetime.now(tz=UTC)
        self._cache.set(MusicBrainzCache(
            id=uuid4(),
            cache_key=cache_key,
            entity_type="artist",
            entity_mbid=mbid,
            response_data=dict(artist),
            cached_at=now,
            expires_at=now + timedelta(days=_CACHE_TTL_DAYS),
        ))

        logger.info("mb_api_lookup_artist", mbid=mbid)
        return artist
