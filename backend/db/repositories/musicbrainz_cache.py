from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from backend.domain.system import MusicBrainzCache
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository

_UPSERT_SQL = """
    INSERT INTO mb_cache (id, cache_key, entity_type, entity_mbid,
                          response_data, cached_at, expires_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (cache_key) DO UPDATE SET
        response_data = EXCLUDED.response_data,
        cached_at = EXCLUDED.cached_at,
        expires_at = EXCLUDED.expires_at
"""


def _upsert_params(cache: MusicBrainzCache) -> tuple[Any, ...]:
    # Jsonb() is psycopg3's type wrapper for jsonb columns: it serialises and
    # registers the right Postgres OID, so orjson can be swapped in later via
    # psycopg.types.json.set_json_dumps() without touching this code.
    return (
        cache.id, cache.cache_key, cache.entity_type, cache.entity_mbid,
        Jsonb(cache.response_data), cache.cached_at, cache.expires_at,
    )


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

    def get_many(self, cache_keys: Sequence[str]) -> dict[str, MusicBrainzCache]:
        if not cache_keys:
            return {}
        rows = self._conn.execute(
            "SELECT * FROM mb_cache WHERE cache_key = ANY(%s) AND expires_at > now()",
            (list(cache_keys),),
        ).fetchall()
        return {row["cache_key"]: self._row_to_model(row) for row in rows}

    def set(self, cache: MusicBrainzCache) -> None:
        self._conn.execute(_UPSERT_SQL, _upsert_params(cache))

    def set_many(self, caches: Sequence[MusicBrainzCache]) -> None:
        if not caches:
            return
        with self._conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, [_upsert_params(c) for c in caches])

    def delete_expired(self) -> int:
        result = self._conn.execute(
            "DELETE FROM mb_cache WHERE expires_at < now()"
        )
        return result.rowcount
