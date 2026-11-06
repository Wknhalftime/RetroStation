"""An artist's names: fake against PostgreSQL (phase 2a S10; C43).

One row per artist, spelling and source, and whether the write added one. The adapter commits
nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.artist_names import PgArtistNameRepository
from backend.db.repositories.artists import PgArtistRepository
from backend.domain.catalog import ArtistName
from backend.domain.enums import ArtistNameSource
from backend.repositories.artist_names import ArtistNameRepository
from tests.fakes.artist_names import FakeArtistNameRepository

pytestmark = pytest.mark.integration

ALIAS = ArtistNameSource.MUSICBRAINZ_ALIAS
TAG = ArtistNameSource.TAG


def _artist(conn: psycopg.Connection[DictRow], normalized: str) -> str:
    row = conn.execute(
        """INSERT INTO artists (id, name, sort_name, normalized_name, origin, needs_enhancement)
           VALUES (%s, %s, %s, %s, 'local', FALSE) RETURNING id""",
        (normalized, normalized, normalized, normalized),
    ).fetchone()
    assert row is not None
    return str(row["id"])


@pytest.fixture(params=["fake", "pg"])
def repo(request: pytest.FixtureRequest) -> Iterator[ArtistNameRepository]:
    if request.param == "fake":
        yield FakeArtistNameRepository()
        return
    migrated_db = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _artist(conn, "artist-1")
        _artist(conn, "artist-2")
        yield PgArtistNameRepository(conn)
        conn.rollback()


def test_a_new_name_is_added(repo: ArtistNameRepository) -> None:
    assert repo.add("artist-1", "The Coop", "coop", ALIAS) is True
    assert repo.list_for_artist("artist-1") == [ArtistName("The Coop", "coop", ALIAS)]


def test_the_same_name_again_adds_nothing(repo: ArtistNameRepository) -> None:
    repo.add("artist-1", "The Coop", "coop", ALIAS)

    assert repo.add("artist-1", "The Coop", "coop", ALIAS) is False
    assert len(repo.list_for_artist("artist-1")) == 1


def test_another_source_or_spelling_is_another_row(repo: ArtistNameRepository) -> None:
    repo.add("artist-1", "The Coop", "coop", ALIAS)

    assert repo.add("artist-1", "The Coop", "coop", TAG) is True
    assert repo.add("artist-1", "THE COOP", "coop", TAG) is True
    assert len(repo.list_for_artist("artist-1")) == 3


def test_two_artists_hold_their_own(repo: ArtistNameRepository) -> None:
    repo.add("artist-1", "The Coop", "coop", ALIAS)

    assert repo.add("artist-2", "The Coop", "coop", ALIAS) is True
    assert repo.list_for_artist("artist-2") == [ArtistName("The Coop", "coop", ALIAS)]


def test_an_artist_with_no_names(repo: ArtistNameRepository) -> None:
    assert repo.list_for_artist("artist-1") == []


def test_names_list_by_source_then_name(repo: ArtistNameRepository) -> None:
    repo.add("artist-1", "Zed", "zed", TAG)
    repo.add("artist-1", "Bee", "bee", TAG)
    repo.add("artist-1", "Yak", "yak", ALIAS)

    assert [name.name for name in repo.list_for_artist("artist-1")] == ["Yak", "Bee", "Zed"]


def test_the_adapter_commits_nothing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist = PgArtistRepository(conn).upsert_local_artist("Someone", "someone")
        conn.commit()

        PgArtistNameRepository(conn).add(artist, "Alias", "alias", ALIAS)

        with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as other:
            assert PgArtistNameRepository(other).list_for_artist(artist) == [
                ArtistName("Someone", "someone", TAG)
            ]
        conn.rollback()
