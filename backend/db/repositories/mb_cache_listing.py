from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import psycopg

from backend.repositories.mb_cache_listing import MusicBrainzCacheListing


class PgMusicBrainzCacheListing(MusicBrainzCacheListing):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def rows(self, cache_keys: Collection[str]) -> dict[str, Mapping[str, object]]:
        if not cache_keys:
            return {}
        rows = self._conn.execute(
            "SELECT cache_key, response_data FROM mb_cache WHERE cache_key = ANY(%s)",
            (list(cache_keys),),
        ).fetchall()
        return {row["cache_key"]: row["response_data"] for row in rows}

    def rows_with_prefix(self, prefix: str) -> list[tuple[str, Mapping[str, object]]]:
        rows = self._conn.execute(
            """SELECT cache_key, response_data FROM mb_cache
                WHERE starts_with(cache_key, %s) ORDER BY cache_key""",
            (prefix,),
        ).fetchall()
        return [(row["cache_key"], row["response_data"]) for row in rows]
