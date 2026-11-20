"""Integration: on an equal score, grouping prefers the MusicBrainz work (AUD-R103, L2).

Where a local and a MusicBrainz work of one artist carry the same title, a new copy of the
song used to land on whichever had the lower id (``_fuzzy_match_work``) or whichever the
artist's work list returned first (``_create_local_work``'s fallback past the fuzzy match's
100 candidates), so about half of them refilled the local work. Now the MusicBrainz work wins
the tie, then the lower id: T14, T15. A better score still wins: T14b.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from tests.integration.song_together_seed import (
    add_file,
    group_like_the_scan,
    insert_local_work,
)

pytestmark = pytest.mark.integration

_WORK = "00000000-0000-4000-8000-0000000b1001"
_LOW_ID = "00000000-0000-4000-8000-000000000001"


def test_a_tie_in_the_fuzzy_match_goes_to_the_musicbrainz_work(migrated_db: str) -> None:
    """T14: both works score 100; the local one has the lower id."""
    title = "Pride (In the Name of Love)"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("U2", normalize_artist("U2"))
        local = insert_local_work(conn, _LOW_ID, title, artist_id)
        mb_work = repos.works.upsert_from_mb(mbid=_WORK, title=title, artist_id=artist_id)
        assert local < mb_work, "the local work would win today's id tie-break"
        new_copy = add_file(repos, "/music/U2/new.flac", artist="U2", title=title)

        assert group_like_the_scan(repos, new_copy) == mb_work


def test_a_better_score_still_wins_over_the_musicbrainz_work(migrated_db: str) -> None:
    """T14b: the preference is for a tie only. "Love Song" scores 100 against the local
    "Love Song" and about 96 against the MusicBrainz "Love Songs" (above the threshold)."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("U2", normalize_artist("U2"))
        local = repos.works.create_local("Love Song", artist_id)
        repos.works.upsert_from_mb(mbid=_WORK, title="Love Songs", artist_id=artist_id)
        new_copy = add_file(repos, "/music/U2/love.flac", artist="U2", title="Love Song")

        assert group_like_the_scan(repos, new_copy) == local


def test_the_fallback_past_the_candidate_cap_prefers_the_musicbrainz_work(
    migrated_db: str,
) -> None:
    """T15: the artist has more than 100 works, so the fuzzy match never sees the two
    "Zebra Song" works; ``_create_local_work``'s exact-title fallback finds both."""
    title = "Zebra Song"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("U2", normalize_artist("U2"))
        for n in range(100):
            repos.works.create_local(f"A{n:03d}", artist_id)
        # The local work is stored first (an unordered read meets it first) and has the
        # lower id (a lowest-id rule picks it): only the MusicBrainz preference passes.
        local = insert_local_work(conn, _LOW_ID, title, artist_id)
        mb_work = repos.works.upsert_from_mb(mbid=_WORK, title=title, artist_id=artist_id)
        assert local < mb_work
        new_copy = add_file(repos, "/music/U2/zebra.flac", artist="U2", title=title)

        landed = group_like_the_scan(repos, new_copy)

        assert landed == mb_work
        assert landed != local
