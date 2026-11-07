"""The backfill's read of ``mb_cache``: fake against PostgreSQL (phase 2a S4, S10).

A new port, which can list and ignores ``expires_at``. An expired row is still what MusicBrainz
said, and the production port's ``get_many`` leaves it out. A key is matched exactly, and a prefix
literally: a name in a search key can hold ``%`` or ``_``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.mb_cache_listing import PgMusicBrainzCacheListing
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.domain.system import MusicBrainzCache
from backend.repositories.mb_cache_listing import MusicBrainzCacheListing
from tests.fakes.mb_cache_listing import FakeMusicBrainzCacheListing

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 6, tzinfo=UTC)


class _Seeded:
    def __init__(self, repo: MusicBrainzCacheListing, seed: Any) -> None:
        self.repo = repo
        self.seed = seed


@pytest.fixture(params=["fake", "pg"])
def cache(request: pytest.FixtureRequest) -> Iterator[_Seeded]:
    if request.param == "fake":
        fake = FakeMusicBrainzCacheListing()

        def seed_fake(key: str, payload: dict[str, Any], *, expired: bool = False) -> None:
            fake.payloads[key] = payload

        yield _Seeded(fake, seed_fake)
        return
    migrated_db = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield _Seeded(PgMusicBrainzCacheListing(conn), _seeder(conn))
        conn.rollback()


def _seeder(conn: psycopg.Connection[DictRow]) -> Any:
    def seed(key: str, payload: dict[str, Any], *, expired: bool = False) -> None:
        kind, _, rest = key.partition(":")
        PgMusicBrainzCacheRepository(conn).set(
            MusicBrainzCache(
                id=uuid4(),
                cache_key=key,
                entity_type=kind,
                entity_mbid=rest,
                response_data=payload,
                cached_at=NOW,
                expires_at=NOW - timedelta(days=1) if expired else NOW + timedelta(days=365),
            )
        )

    return seed


def test_rows_by_key(cache: _Seeded) -> None:
    cache.seed("artist:one", {"name": "A"})
    cache.seed("artist:two", {"name": "B"})

    assert cache.repo.rows(["artist:one", "artist:gone"]) == {"artist:one": {"name": "A"}}
    assert cache.repo.rows([]) == {}


def test_an_expired_row_is_still_read(cache: _Seeded) -> None:
    cache.seed("artist:old", {"name": "Old"}, expired=True)

    assert cache.repo.rows(["artist:old"]) == {"artist:old": {"name": "Old"}}
    assert cache.repo.rows_with_prefix("artist:") == [("artist:old", {"name": "Old"})]


def test_rows_with_a_prefix(cache: _Seeded) -> None:
    cache.seed("artist:b", {"name": "B"})
    cache.seed("artist:a", {"name": "A"})
    cache.seed("artist-search:a", {"artists": []})
    cache.seed("recording:a", {"title": "Song"})

    assert cache.repo.rows_with_prefix("artist:") == [
        ("artist:a", {"name": "A"}),
        ("artist:b", {"name": "B"}),
    ]
    assert cache.repo.rows_with_prefix("artist-search:") == [("artist-search:a", {"artists": []})]
    assert cache.repo.rows_with_prefix("nothing:") == []


def test_a_prefix_is_literal(cache: _Seeded) -> None:
    cache.seed("artist-search:100%", {"artists": []})
    cache.seed("artist-search:a_b", {"artists": []})
    cache.seed("artist-search:axb", {"artists": []})

    assert [key for key, _ in cache.repo.rows_with_prefix("artist-search:100%")] == [
        "artist-search:100%"
    ]
    assert [key for key, _ in cache.repo.rows_with_prefix("artist-search:a_")] == [
        "artist-search:a_b"
    ]
