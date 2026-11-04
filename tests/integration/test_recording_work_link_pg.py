"""Integration: the guarded recording writes behind ``apply_recording_lookup``.

``uq_recordings_work_version`` allows one recording per (work, version), so
linking a recording to a work, or changing its version while it is on one,
must refuse when the slot is taken instead of raising inside the enrichment
pass (346 of the 7,026 recordings the 2026-11-02 profile would link share a
slot with another). Postgres is the authority; ``tests/fakes/recordings.py``
mirrors these rules.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.catalog import Recording
from backend.domain.enums import CatalogSource, EnrichmentStatus, VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import EnrichmentRepos, apply_recording_lookup
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration

_ARTIST = "00000000-0000-4000-8000-00000000b001"
_WORK = "00000000-0000-4000-8000-00000000b002"
_REC_A = "00000000-0000-4000-8000-00000000b003"
_REC_B = "00000000-0000-4000-8000-00000000b004"


def _seed(conn: psycopg.Connection[dict[str, Any]]) -> tuple[RepositoryFactory, str]:
    repos = RepositoryFactory(conn)
    artist_id = repos.artists.upsert_musicbrainz_artist(
        mbid=_ARTIST, name="Aerosmith", sort_name="Aerosmith", normalized_name="aerosmith"
    )
    work_id = repos.works.upsert_from_mb(mbid=_WORK, title="Dream On", artist_id=artist_id)
    repos.recordings.upsert(Recording(id=_REC_A, title="Dream On"))
    repos.recordings.upsert(Recording(id=_REC_B, title="Dream On (remastered)"))
    return repos, work_id


def test_link_work_takes_a_free_version_slot_and_refuses_a_taken_one(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, work_id = _seed(conn)

        assert repos.recordings.link_work(_REC_A, work_id) is True
        assert repos.recordings.link_work(_REC_B, work_id) is False
        # The refused link raised nothing, so the transaction is still usable.
        assert repos.recordings.link_work(_REC_A, work_id) is True, "relinking is idempotent"

        a = repos.recordings.get_by_id(_REC_A)
        b = repos.recordings.get_by_id(_REC_B)
        assert a is not None and a.work_id == work_id
        assert b is not None and b.work_id is None


def test_update_version_type_refuses_a_slot_another_recording_holds(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, work_id = _seed(conn)
        repos.recordings.link_work(_REC_A, work_id)
        repos.recordings.upsert(
            Recording(
                id="rec-live", title="Dream On", work_id=work_id, version_type=VersionType.LIVE
            )
        )

        assert repos.recordings.update_version_type(_REC_A, VersionType.LIVE) is False
        # Off any work there is no slot to clash with.
        assert repos.recordings.update_version_type(_REC_B, VersionType.LIVE) is True

        a = repos.recordings.get_by_id(_REC_A)
        b = repos.recordings.get_by_id(_REC_B)
        assert a is not None and a.version_type == VersionType.ORIGINAL
        assert b is not None and b.version_type == VersionType.LIVE


def test_update_duration_and_musicbrainz_work_listing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, work_id = _seed(conn)
        local_work = repos.works.create_local(
            "Dream On", repos.artists.upsert_local_artist("Aerosmith", "aerosmith")
        )

        repos.recordings.update_duration(_REC_A, 291000)
        a = repos.recordings.get_by_id(_REC_A)
        assert a is not None and a.duration_ms == 291000

        assert repos.works.list_musicbrainz_ids([work_id, local_work, "no-such-work"]) == {work_id}
        assert repos.works.list_musicbrainz_ids([]) == set()


def test_apply_recording_lookup_moves_the_file_and_settles_the_local_work(
    migrated_db: str,
) -> None:
    """End to end on Postgres: the pass's apply step, on a file grouping put on a local work."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        local_artist = repos.artists.upsert_local_artist("Aerosmith", "aerosmith")
        local_work = repos.works.create_local("Dream On", local_artist)
        recording = repos.recordings.upsert(Recording(id=_REC_A, title="Dream On"))
        lf = repos.library_files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path="/music/dream-on.flac",
                format="flac",
                enrichment_status=EnrichmentStatus.ENRICHED,
                recording_id=_REC_A,
                work_id=local_work,
                audio=AudioMetadata(recording_mbid=_REC_A),
            )
        )
        payload: dict[str, Any] = {
            "id": _REC_A,
            "title": "Dream On",
            "length": 291000,
            "artist-credit": [
                {"artist": {"id": _ARTIST, "name": "Aerosmith", "sort-name": "Aerosmith"}}
            ],
            "relations": [
                {
                    "type": "performance",
                    "attributes": ["live"],
                    "work": {"id": _WORK, "title": "Dream On"},
                }
            ],
        }
        enrichment_repos = EnrichmentRepos(
            files=repos.library_files,
            enrichment_queries=repos.library_files,
            recordings=repos.recordings,
            works=repos.works,
            song_masters=repos.song_masters,
            matches=repos.matches,
            artists=repos.artists,
        )

        applied = apply_recording_lookup(recording, payload, enrichment_repos)
        conn.commit()

        assert applied.work_id is not None
        work = repos.works.get_by_id(applied.work_id)
        assert work is not None
        assert work.mbid == _WORK and work.origin == CatalogSource.MUSICBRAINZ
        stored = repos.recordings.get_by_id(_REC_A)
        assert stored is not None
        assert stored.work_id == work.id
        assert stored.version_type == VersionType.LIVE
        assert stored.duration_ms == 291000
        moved = repos.library_files.get_by_id(lf.id)
        assert moved is not None and moved.work_id == work.id
        assert repos.works.get_by_id(local_work) is None, "the emptied local work is settled"
        master = repos.song_masters.get_by_work(work.id)
        assert master is not None and master.preferred_file_id == lf.id
