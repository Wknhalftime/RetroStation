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


def test_upsert_complete_fields_clears_needs_enhancement(pg_conn):
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-complete",
        name="Fully Known",
        sort_name="Known, Fully",
        normalized_name="fully known",
        disambiguation="Definitely the right one",
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row["needs_enhancement"] is False


def test_upsert_missing_disambiguation_keeps_needs_enhancement(pg_conn):
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-partial",
        name="Partial Artist",
        sort_name="Artist, Partial",
        normalized_name="partial artist",
        disambiguation=None,
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row["needs_enhancement"] is True


def test_upsert_sort_name_equals_name_keeps_needs_enhancement(pg_conn):
    """sort_name that merely echoes name isn't a real MB sort form."""
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-madonna",
        name="Madonna",
        sort_name="Madonna",
        normalized_name="madonna",
        disambiguation="American singer",
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row["needs_enhancement"] is True
