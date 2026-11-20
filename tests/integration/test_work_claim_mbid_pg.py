"""Integration: the work ports behind R1 of AUD-R103 ("Relabel + leaks"): T9.

``claim_mbid`` gives a local work an MBID no work holds yet; it refuses, without raising,
anything else, so the enrichment step can fall back to today's path inside its transaction.
``get_id_by_mbid`` reads which work, if any, holds an MBID.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from tests.integration.song_together_seed import work_row

pytestmark = pytest.mark.integration

_WORK = "00000000-0000-4000-8000-0000000c1001"
_HELD = "00000000-0000-4000-8000-0000000c1002"


def test_claim_mbid_makes_a_local_work_the_musicbrainz_work(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("U2", normalize_artist("U2"))
        local = repos.works.create_local("Pride (In The Name Of Love)", artist_id)

        assert repos.works.claim_mbid(local, _WORK, "Pride (In the Name of Love)") is True

        row = work_row(conn, local)
        assert row is not None
        assert row["mbid"] == _WORK
        assert row["origin"] == "musicbrainz"
        assert row["title"] == "Pride (In the Name of Love)"
        assert row["needs_enhancement"] is True
        assert row["artist_id"] == artist_id
        assert repos.works.get_id_by_mbid(_WORK) == local


@pytest.mark.parametrize("case", ["musicbrainz-work", "mbid-held-elsewhere", "no-such-work"])
def test_claim_mbid_refuses_without_raising(migrated_db: str, case: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("U2", normalize_artist("U2"))
        local = repos.works.create_local("One", artist_id)
        held_by = repos.works.upsert_from_mb(mbid=_HELD, title="One", artist_id=artist_id)
        target, mbid = {
            "musicbrainz-work": (held_by, _WORK),
            "mbid-held-elsewhere": (local, _HELD),
            "no-such-work": ("00000000-0000-4000-8000-0000000c10ff", _WORK),
        }[case]

        assert repos.works.claim_mbid(target, mbid, "One") is False

        # Nothing changed, and the transaction is still usable.
        local_row = work_row(conn, local)
        assert local_row is not None and local_row["mbid"] is None
        assert local_row["origin"] == "local"
        held_row = work_row(conn, held_by)
        assert held_row is not None and held_row["mbid"] == _HELD
        assert repos.works.get_id_by_mbid(_WORK) is None
        assert repos.works.get_id_by_mbid(_HELD) == held_by
