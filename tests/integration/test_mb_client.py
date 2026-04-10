from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.mb_cache import PgMusicBrainzCacheRepository
from backend.services.mb_client import RealMbClient


def test_mb_search_artist_real_api(migrated_db: str) -> None:
    """Integration test: real MusicBrainz API call for a known artist."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        cache_repo = PgMusicBrainzCacheRepository(conn)
        client = RealMbClient(cache_repo)

        # First call: hits API
        results = client.search_artist("Metallica")
        assert len(results) > 0
        assert any(r.get("name") == "Metallica" for r in results)

        conn.commit()

        # Second call: should hit cache
        results2 = client.search_artist("Metallica")
        assert len(results2) > 0

        conn.commit()
