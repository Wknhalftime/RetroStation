from __future__ import annotations

import os

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.services.mb_client import MusicBrainzApiClient


def test_mb_search_artist_cache_hit(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache-hit path: second call returns cached data without an HTTP round-trip."""
    canned = {"artists": [{"id": "some-mbid", "name": "Metallica", "score": 100}]}
    call_count = 0

    def _fake_fetch(
        self: MusicBrainzApiClient, url: str, params: dict[str, str]
    ) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=canned)

    monkeypatch.setattr(
        "backend.services.mb_client.MusicBrainzApiClient._fetch", _fake_fetch
    )

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        cache_repo = PgMusicBrainzCacheRepository(conn)
        client = MusicBrainzApiClient(cache_repo)

        first = client.search_artist("Metallica")
        assert any(r.get("name") == "Metallica" for r in first)
        conn.commit()

        second = client.search_artist("Metallica")
        assert second == first
        conn.commit()

    assert call_count == 1  # second call served from cache, not _fetch


@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_MB_LIVE") != "1",
    reason="Live MusicBrainz API test; set RUN_MB_LIVE=1 to enable",
)
def test_mb_search_artist_real_api(migrated_db: str) -> None:
    """Integration test: real MusicBrainz API call for a known artist."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        cache_repo = PgMusicBrainzCacheRepository(conn)
        client = MusicBrainzApiClient(cache_repo)

        results = client.search_artist("Metallica")
        assert len(results) > 0
        assert any(r.get("name") == "Metallica" for r in results)

        conn.commit()

        results2 = client.search_artist("Metallica")
        assert len(results2) > 0

        conn.commit()
