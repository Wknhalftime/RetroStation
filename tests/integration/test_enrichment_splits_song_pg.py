"""Integration: enriching one copy of a song must not split it from the copies left behind.

Reproduces the MusicBrainz-work + local-work pairs (1,055 on the 2026-11-19 gate copy of
the dev DB, e.g. U2 "Pride"). Grouping puts every copy of a song on one local work. When a
recording lookup names the work a copy performs, ``apply_recording_lookup`` creates the
MusicBrainz work by MBID alone (``PgWorkRepository.upsert_from_mb``) and moves only that
recording's files onto it (``work_move_service.move_file_to_work``). A copy whose own
recording names no work, here an untagged copy, stays on the local work, so the library now
holds two works of one artist and title.

This states the invariant, not a fix: which fix (and so the exact assertion) is Lance's
ruling. The steps are the ones the pipeline runs, in its order: the scan's grouping pass
(``library_scan_tasks``), enrichment pass 1 (``enrich_by_recording_batch``), then the
recordings phase's apply step (``mb_enrichment_tasks._run_recordings_phase``; #227's
backfill script runs the same step over cached payloads).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.grouping_service import assign_work
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    apply_recording_lookup,
    enrich_by_recording_batch,
)
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_move_service import place_ungrouped_file
from tests.fakes.mb_client import FakeMbClient

pytestmark = pytest.mark.integration

_ARTIST_MBID = "a3cb23fc-acd3-4ce0-8f36-1e5aa6a18432"
_WORK_MBID = "00000000-0000-4000-8000-0000000c0001"
_RECORDING_MBID = "00000000-0000-4000-8000-0000000c0002"
_RELEASE_MBID = "00000000-0000-4000-8000-0000000c0003"
_TITLE = "Pride (In the Name of Love)"


def _file(path: str, audio: AudioMetadata) -> LibraryFile:
    return LibraryFile(id=uuid4(), file_path=path, format="flac", audio=audio)


def _group_like_the_scan(repos: RepositoryFactory, library_file: LibraryFile) -> None:
    """The scan's grouping pass for one new file (``library_scan_tasks._run_scan``)."""
    result = assign_work(
        library_file,
        artist_repo=repos.artists,
        work_repo=repos.works,
        library_file_repo=repos.library_files,
        recording_repo=repos.recordings,
        song_master_repo=repos.song_masters,
    )
    assert result is not None
    place_ungrouped_file(library_file.id, result.work_id, repos.library_files, repos.matches)
    if result.recording_id:
        repos.library_files.update_recording_link(
            library_file.id, result.recording_id, EnrichmentStatus.PENDING
        )


def _recording_payload() -> dict[str, Any]:
    """What MusicBrainz returns for the album copy's recording: on the release, credited to
    U2, performing the work (search hits carry no relations; the lookup does)."""
    return {
        "id": _RECORDING_MBID,
        "title": _TITLE,
        "length": 228_000,
        "artist-credit": [{"artist": {"id": _ARTIST_MBID, "name": "U2", "sort-name": "U2"}}],
        "releases": [{"id": _RELEASE_MBID}],
        "relations": [
            {"type": "performance", "work": {"id": _WORK_MBID, "title": _TITLE}},
        ],
    }


def test_enriching_the_tagged_copy_leaves_the_song_on_one_work(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        enrichment_repos = EnrichmentRepos(
            files=repos.library_files,
            enrichment_queries=repos.library_files,
            recordings=repos.recordings,
            works=repos.works,
            song_masters=repos.song_masters,
            matches=repos.matches,
            artists=repos.artists,
        )
        tagged = repos.library_files.upsert(
            _file(
                "/music/U2/The Unforgettable Fire/03 Pride.flac",
                AudioMetadata(
                    artist_name="U2",
                    track_title=_TITLE,
                    duration_ms=228_000,
                    recording_mbid=_RECORDING_MBID,
                    release_mbid=_RELEASE_MBID,
                ),
            )
        )
        untagged = repos.library_files.upsert(
            _file(
                "/music/U2/Best Of 1980-1990/02 Pride.flac",
                AudioMetadata(artist_name="U2", track_title=_TITLE, duration_ms=229_000),
            )
        )
        for library_file in (tagged, untagged):
            _group_like_the_scan(repos, library_file)
        conn.commit()

        grouped_tagged = repos.library_files.get_by_id(tagged.id)
        grouped_untagged = repos.library_files.get_by_id(untagged.id)
        assert grouped_tagged is not None and grouped_untagged is not None
        assert grouped_tagged.work_id is not None
        assert grouped_tagged.work_id == grouped_untagged.work_id, "grouping: one song, one work"

        # Enrichment pass 1: the batched search links the tagged copy to its recording.
        client = FakeMbClient(recordings={_RECORDING_MBID: _recording_payload()})
        outcome = enrich_by_recording_batch([grouped_tagged], enrichment_repos, client)
        assert outcome.enriched == 1
        conn.commit()

        # The recordings phase: the lookup names the performed work, and it is applied.
        recording = repos.recordings.get_by_id(_RECORDING_MBID)
        assert recording is not None
        applied = apply_recording_lookup(recording, _recording_payload(), enrichment_repos)
        conn.commit()
        assert applied.files_moved == 1

        after_tagged = repos.library_files.get_by_id(tagged.id)
        after_untagged = repos.library_files.get_by_id(untagged.id)
        assert after_tagged is not None and after_untagged is not None
        works = conn.execute(
            """SELECT w.id, w.origin, count(lf.id) AS files
               FROM works w LEFT JOIN library_files lf ON lf.work_id = w.id
               WHERE w.title = %s GROUP BY w.id, w.origin ORDER BY w.origin""",
            (_TITLE,),
        ).fetchall()
        assert after_tagged.work_id == after_untagged.work_id, (
            "enrichment split the song: the tagged copy is on the MusicBrainz work, the "
            f"untagged copy stayed on the local one; works titled {_TITLE!r}: "
            + ", ".join(f"{w['origin']} ({w['files']} file)" for w in works)
        )
        assert len(works) == 1
