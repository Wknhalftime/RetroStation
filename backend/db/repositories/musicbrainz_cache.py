from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from backend.domain.system import MusicBrainzCache
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository


class PgMusicBrainzCacheRepository(MusicBrainzCacheRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> MusicBrainzCache:
        # psycopg3 decodes JSONB columns into Python objects directly — no
        # str-to-dict fallback needed.
        return MusicBrainzCache(
            id=row["id"],
            cache_key=row["cache_key"],
            entity_type=row["entity_type"],
            entity_mbid=row["entity_mbid"],
            response_data=row["response_data"],
            cached_at=row["cached_at"],
            expires_at=row["expires_at"],
        )

    def get(self, cache_key: str) -> MusicBrainzCache | None:
        row = self._conn.execute(
            "SELECT * FROM mb_cache WHERE cache_key = %s AND expires_at > now()",
            (cache_key,),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def set(self, cache: MusicBrainzCache) -> None:
        # Jsonb() is psycopg3's type wrapper for jsonb columns — handles
        # serialization (default `json.dumps`) and registers the right
        # Postgres OID. Using it instead of a manual `json.dumps()` keeps
        # the call site type-safe and lets us swap in `orjson` later via
        # `psycopg.types.json.set_json_dumps()` without touching this code.
        self._conn.execute(
            """INSERT INTO mb_cache (id, cache_key, entity_type, entity_mbid,
               response_data, cached_at, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (cache_key) DO UPDATE SET
               response_data = EXCLUDED.response_data,
               cached_at = EXCLUDED.cached_at,
               expires_at = EXCLUDED.expires_at""",
            (cache.id, cache.cache_key, cache.entity_type, cache.entity_mbid,
             Jsonb(cache.response_data), cache.cached_at, cache.expires_at),
        )

    def delete_expired(self) -> int:
        result = self._conn.execute(
            "DELETE FROM mb_cache WHERE expires_at < now()"
        )
        return result.rowcount
