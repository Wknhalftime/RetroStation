"""Integration: the untagged-file pass on Postgres.

The candidate listing is a join (file -> work -> artist) the fakes only mirror,
so Postgres is the authority for which files it lists; and a confident pick
is then written through the real repositories: recording link, work move.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import EnrichmentRepos
from backend.services.repository_factory import RepositoryFactory
from backend.services.untagged_recording_service import enrich_untagged_pair, untagged_pairs
from tests.fakes.mb_client import FakeMbClient

pytestmark = pytest.mark.integration

_ARTIST = "f9776598-2689-41db-98d3-829f5510021c"
_REC = "00000000-0000-4000-8000-00000000d001"
_REC_2 = "00000000-0000-4000-8000-00000000d002"
_WORK = "00000000-0000-4000-8000-00000000d003"
_CREDIT = [{"artist": {"id": _ARTIST, "name": "Jesus Jones", "sort-name": "Jesus Jones"}}]


def _file(
    path: str,
    work_id: str | None,
    *,
    title: str = "Right Here, Right Now",
    status: EnrichmentStatus = EnrichmentStatus.PENDING,
    recording_mbid: str | None = None,
    release_mbid: str | None = None,
    file_status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        format="flac",
        enrichment_status=status,
        file_status=file_status,
        work_id=work_id,
        audio=AudioMetadata(
            track_title=title,
            duration_ms=183_000,
            recording_mbid=recording_mbid,
            release_mbid=release_mbid,
        ),
    )


def _seed(conn: psycopg.Connection[dict[str, Any]]) -> tuple[RepositoryFactory, str, str]:
    repos = RepositoryFactory(conn)
    linked = repos.artists.upsert_musicbrainz_artist(
        mbid=_ARTIST, name="Jesus Jones", sort_name="Jesus Jones", normalized_name="jesus jones"
    )
    unlinked = repos.artists.upsert_local_artist("Nobody", "nobody")
    return repos, repos.works.create_local("Right Here, Right Now", linked), unlinked


def test_lists_untagged_present_files_whose_artist_has_an_mbid(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, linked_work, unlinked = _seed(conn)
        unlinked_work = repos.works.create_local("Right Here, Right Now", unlinked)
        files = repos.library_files

        b = files.upsert(_file("/m/b.flac", linked_work))
        a = files.upsert(_file("/m/a.flac", linked_work))
        files.upsert(_file("/m/i.flac", linked_work, status=EnrichmentStatus.FAILED))
        files.upsert(_file("/m/c.flac", linked_work, recording_mbid=_REC))
        files.upsert(_file("/m/d.flac", linked_work, release_mbid="some-release"))
        files.upsert(_file("/m/e.flac", unlinked_work))
        files.upsert(_file("/m/f.flac", linked_work, status=EnrichmentStatus.ENRICHED))
        files.upsert(_file("/m/g.flac", None))
        missing = files.upsert(_file("/m/h.flac", linked_work))
        files.mark_missing(missing.file_path)

        listed = files.list_untagged_with_linked_artist()

        assert [(c.file.id, c.artist_mbid) for c in listed] == [(a.id, _ARTIST), (b.id, _ARTIST)]
        assert listed[0].file.audio.track_title == "Right Here, Right Now"


def test_confident_pick_links_the_recording_and_moves_the_file_to_its_work(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, linked_work, _ = _seed(conn)
        sure = repos.library_files.upsert(_file("/m/sure.flac", linked_work))
        unsure = repos.library_files.upsert(
            _file("/m/unsure.flac", linked_work, title="International Bright Young Thing")
        )
        enrichment_repos = EnrichmentRepos(
            files=repos.library_files,
            enrichment_queries=repos.library_files,
            recordings=repos.recordings,
            works=repos.works,
            song_masters=repos.song_masters,
            matches=repos.matches,
            artists=repos.artists,
        )
        client = FakeMbClient(
            recording_searches={
                (_ARTIST, "Right Here, Right Now"): [
                    {
                        "id": _REC,
                        "title": "Right Here, Right Now",
                        "length": 183_300,
                        "artist-credit": _CREDIT,
                    },
                ],
                (_ARTIST, "International Bright Young Thing"): [
                    {
                        "id": _REC_2,
                        "title": "International Bright Young Thing",
                        "length": 183_000,
                        "artist-credit": _CREDIT,
                    },
                    {
                        "id": "r3",
                        "title": "International Bright Young Thing",
                        "length": 184_000,
                        "artist-credit": _CREDIT,
                    },
                ],
            },
            recordings={
                _REC: {
                    "id": _REC,
                    "title": "Right Here, Right Now",
                    "length": 183_000,
                    "artist-credit": _CREDIT,
                    "relations": [
                        {
                            "type": "performance",
                            "attributes": [],
                            "work": {"id": _WORK, "title": "Right Here, Right Now"},
                        }
                    ],
                }
            },
        )

        pairs = untagged_pairs(repos.library_files.list_untagged_with_linked_artist())
        outcomes = [enrich_untagged_pair(p, enrichment_repos, client) for p in pairs]
        conn.commit()

        assert [o.enriched for o in outcomes] == [1, 0]
        stored_sure = repos.library_files.get_by_id(sure.id)
        assert stored_sure is not None
        assert stored_sure.enrichment_status is EnrichmentStatus.ENRICHED
        assert stored_sure.recording_id == _REC
        assert stored_sure.work_id is not None
        moved_to = repos.works.get_by_id(stored_sure.work_id)
        assert moved_to is not None and moved_to.mbid == _WORK, "moved onto the MusicBrainz work"
        stored_unsure = repos.library_files.get_by_id(unsure.id)
        assert stored_unsure is not None
        assert stored_unsure.enrichment_status is EnrichmentStatus.PENDING
        assert stored_unsure.recording_id is None
        assert stored_unsure.work_id == linked_work
        # The undecided file is a candidate again on the next run; the linked one is not.
        assert [c.file.id for c in repos.library_files.list_untagged_with_linked_artist()] == [
            unsure.id
        ]
