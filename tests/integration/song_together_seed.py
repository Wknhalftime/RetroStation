"""Seed helpers for the AUD-R103 tests: one song's copies through grouping and enrichment.

Each helper does what the pipeline does, in its order: the scan stores a file and its grouping
pass gives it a work (``library_scan_tasks._run_scan``); enrichment then links it. Nothing here
calls MusicBrainz: payloads are built in the shape the client returns.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.grouping_service import assign_work
from backend.services.library_enrichment_service import EnrichmentRepos
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_move_service import place_ungrouped_file


def enrichment_repos(repos: RepositoryFactory) -> EnrichmentRepos:
    return EnrichmentRepos(
        files=repos.library_files,
        enrichment_queries=repos.library_files,
        recordings=repos.recordings,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
        artists=repos.artists,
    )


def add_file(
    repos: RepositoryFactory,
    path: str,
    *,
    artist: str,
    title: str,
    duration_ms: int | None = None,
    recording_mbid: str | None = None,
    release_mbid: str | None = None,
    file_id: UUID | None = None,
) -> LibraryFile:
    """Store a file as the scan does: pending, no work yet."""
    return repos.library_files.upsert(
        LibraryFile(
            id=file_id or uuid4(),
            file_path=path,
            format="flac",
            audio=AudioMetadata(
                artist_name=artist,
                track_title=title,
                duration_ms=duration_ms,
                recording_mbid=recording_mbid,
                release_mbid=release_mbid,
            ),
        )
    )


def group_like_the_scan(repos: RepositoryFactory, library_file: LibraryFile) -> str:
    """The scan's grouping pass for one stored file; returns the work it was given."""
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
    return result.work_id


def stored(repos: RepositoryFactory, file_id: UUID) -> LibraryFile:
    row = repos.library_files.get_by_id(file_id)
    assert row is not None
    return row


def credit(artist_mbid: str, name: str) -> list[dict[str, Any]]:
    return [{"artist": {"id": artist_mbid, "name": name, "sort-name": name}}]


def recording_payload(
    recording_mbid: str,
    *,
    title: str,
    length: int,
    artist_credit: list[dict[str, Any]],
    work_mbid: str,
    work_title: str,
    release_mbid: str | None = None,
) -> dict[str, Any]:
    """A recording as the lookup returns it (with its performance relation). Pass 1's
    batched search reads the same dict: it uses ``releases`` and ignores ``relations``."""
    payload: dict[str, Any] = {
        "id": recording_mbid,
        "title": title,
        "length": length,
        "artist-credit": artist_credit,
        "relations": [{"type": "performance", "work": {"id": work_mbid, "title": work_title}}],
    }
    if release_mbid is not None:
        payload["releases"] = [{"id": release_mbid}]
    return payload


def work_row(conn: psycopg.Connection[Any], work_id: str) -> dict[str, Any] | None:
    row: dict[str, Any] | None = conn.execute(
        """SELECT id, title, artist_id, mbid, origin, needs_enhancement
           FROM works WHERE id = %s""",
        (work_id,),
    ).fetchone()
    return row


def works_of_artist(conn: psycopg.Connection[Any], artist_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = conn.execute(
        "SELECT id, title, mbid, origin FROM works WHERE artist_id = %s ORDER BY id",
        (artist_id,),
    ).fetchall()
    return rows


def recordings_on(conn: psycopg.Connection[Any], work_id: str) -> list[str]:
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM recordings WHERE work_id = %s ORDER BY id", (work_id,)
        ).fetchall()
    ]


def insert_local_work(
    conn: psycopg.Connection[Any], work_id: str, title: str, artist_id: str
) -> str:
    """A local work with a chosen id, as grouping's ``create_local`` makes one (for tests that
    need the ids in a known order)."""
    conn.execute(
        """INSERT INTO works (id, title, artist_id, origin, needs_enhancement)
           VALUES (%s, %s, %s, 'local', FALSE)""",
        (work_id, title, artist_id),
    )
    return work_id
