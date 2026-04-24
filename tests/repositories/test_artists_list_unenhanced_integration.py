from __future__ import annotations

import psycopg
import pytest

from backend.db.repositories.artists import PgArtistRepository

pytestmark = pytest.mark.integration


def test_failed_artist_not_returned_by_list_unenhanced(
    pg_conn: psycopg.Connection[dict[str, object]],
) -> None:
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-bad",
        name="Bad MBID Artist",
        sort_name="Bad MBID Artist",
        normalized_name="bad mbid artist",
        disambiguation=None,
    )
    repo.mark_enhancement_failed(artist_id, "MB lookup returned 404 for mbid=mb-bad")

    ids = [a.id for a in repo.list_unenhanced()]
    assert artist_id not in ids
