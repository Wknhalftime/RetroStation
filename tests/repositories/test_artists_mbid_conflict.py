import pytest

from backend.db.repositories.artists import PgArtistRepository

pytestmark = pytest.mark.integration


def test_upsert_same_mbid_returns_existing_id(pg_conn):
    repo = PgArtistRepository(pg_conn)
    first_id = repo.upsert_musicbrainz_artist(
        mbid="mb-A",
        name="Alice",
        sort_name="Alice",
        normalized_name="alice",
        disambiguation="singer",
    )
    second_id = repo.upsert_musicbrainz_artist(
        mbid="mb-A",
        name="Alice",
        sort_name="Alice",
        normalized_name="alice",
        disambiguation="singer",
    )
    assert first_id == second_id


def test_upsert_conflicting_mbid_does_not_overwrite(pg_conn):
    repo = PgArtistRepository(pg_conn)
    original_id = repo.upsert_musicbrainz_artist(
        mbid="mb-A",
        name="Alice",
        sort_name="Alice",
        normalized_name="alice",
        disambiguation="singer",
    )
    returned_id = repo.upsert_musicbrainz_artist(
        mbid="mb-B",
        name="Alice",
        sort_name="Alice",
        normalized_name="alice",
        disambiguation="singer",
    )
    assert returned_id == original_id
    row = pg_conn.execute(
        "SELECT mbid FROM artists WHERE id = %s", (original_id,)
    ).fetchone()
    assert row["mbid"] == "mb-A"  # NOT overwritten
