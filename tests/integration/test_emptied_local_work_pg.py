"""Integration: a local work a move empties is deleted (AUD-R103, L1): T11-T13.

Grouping gives each file a recording on its local work. Enrichment re-points the file to its
MusicBrainz recording, so grouping's recording is left without files; it used to keep the
emptied work alive (``delete_if_empty`` refuses a work any recording points at), and that left
about 9,680 empty local works on the 2026-11-19 gate copy. When a file leaves a local work,
grouping's file-less recordings of it go first; then the work is deleted unless something else
still points at it (a match or a format override), as before. A MusicBrainz work is not
touched by this.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import TargetType
from backend.services.library_enrichment_service import (
    apply_recording_lookup,
    enrich_by_recording_batch,
)
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work
from tests.fakes.mb_client import FakeMbClient
from tests.integration.song_together_seed import (
    add_file,
    credit,
    enrichment_repos,
    group_like_the_scan,
    recording_payload,
    recordings_on,
    stored,
    work_row,
)

pytestmark = pytest.mark.integration

_U2 = "a3cb23fc-acd3-4ce0-8f36-1e5aa6a18432"
_WORK = "00000000-0000-4000-8000-0000000d1001"
_RECORDING = "00000000-0000-4000-8000-0000000d1002"
_RELEASE = "00000000-0000-4000-8000-0000000d1003"
_TITLE = "Pride (In the Name of Love)"


def _payload() -> dict[str, Any]:
    return recording_payload(
        _RECORDING,
        title=_TITLE,
        length=228_000,
        artist_credit=credit(_U2, "U2"),
        work_mbid=_WORK,
        work_title=_TITLE,
        release_mbid=_RELEASE,
    )


def _lone_copy_on_a_local_work_with_the_mb_work_made(
    conn: psycopg.Connection[DictRow],
) -> tuple[RepositoryFactory, UUID, str, str]:
    """A lone tagged copy grouped onto a local work; another album's copy has already made
    the MusicBrainz work, so R1 does not apply and the copy moves alone."""
    repos = RepositoryFactory(conn)
    lone = add_file(
        repos,
        "/music/U2/UF/03.flac",
        artist="U2",
        title=_TITLE,
        duration_ms=228_000,
        recording_mbid=_RECORDING,
        release_mbid=_RELEASE,
    )
    local = group_like_the_scan(repos, lone)
    local_row = work_row(conn, local)
    assert local_row is not None
    existing = repos.works.upsert_from_mb(
        mbid=_WORK, title=_TITLE, artist_id=local_row["artist_id"]
    )
    conn.commit()
    return repos, lone.id, local, existing


def _enrich_the_copy(repos: RepositoryFactory, file_id: UUID) -> None:
    client = FakeMbClient(recordings={_RECORDING: _payload()})
    batch = enrich_by_recording_batch([stored(repos, file_id)], enrichment_repos(repos), client)
    assert batch.enriched == 1
    recording = repos.recordings.get_by_id(_RECORDING)
    assert recording is not None
    apply_recording_lookup(recording, _payload(), enrichment_repos(repos))


def test_a_local_work_emptied_by_enrichment_is_deleted(migrated_db: str) -> None:
    """T11."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, file_id, local, existing = _lone_copy_on_a_local_work_with_the_mb_work_made(conn)

        _enrich_the_copy(repos, file_id)
        conn.commit()

        assert stored(repos, file_id).work_id == existing
        assert work_row(conn, local) is None, "no empty local work is left behind"
        assert recordings_on(conn, local) == []


@pytest.mark.parametrize("reference", ["format-override", "match-target"])
def test_an_emptied_local_work_something_still_points_at_stays(
    migrated_db: str, reference: str
) -> None:
    """T12: the deletion rule is unchanged; only grouping's file-less recordings stop
    counting."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, file_id, local, existing = _lone_copy_on_a_local_work_with_the_mb_work_made(conn)
        if reference == "format-override":
            conn.execute(
                """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
                   VALUES (%s, %s, %s)""",
                (local, "classic", file_id),
            )
        else:
            artist = repos.broadcast_artists.upsert(
                BroadcastArtist(id=uuid4(), original_name="U2", normalized_name=str(uuid4()))
            )
            conn.execute(
                """INSERT INTO matches (artist_id, library_file_id, target_id, target_type)
                   VALUES (%s, %s, %s, %s)""",
                (artist.id, file_id, local, TargetType.WORK.value),
            )
        conn.commit()

        _enrich_the_copy(repos, file_id)
        conn.commit()

        assert stored(repos, file_id).work_id == existing
        assert work_row(conn, local) is not None


def test_a_musicbrainz_work_keeps_grouping_recordings_when_emptied(migrated_db: str) -> None:
    """T13: L1 is for local works only. A file grouped onto a MusicBrainz work by title gets
    a grouping recording there; when the file moves away, the work and that recording stay,
    as before."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_musicbrainz_artist(
            mbid=_U2, name="U2", sort_name="U2", normalized_name=normalize_artist("U2")
        )
        mb_work = repos.works.upsert_from_mb(mbid=_WORK, title=_TITLE, artist_id=artist_id)
        copy = add_file(repos, "/music/U2/x.flac", artist="U2", title=_TITLE)
        assert group_like_the_scan(repos, copy) == mb_work
        grouping_recordings = recordings_on(conn, mb_work)
        assert len(grouping_recordings) == 1
        elsewhere = repos.works.create_local("Somewhere Else", artist_id)
        conn.commit()

        move_file_to_work(
            stored(repos, copy.id),
            elsewhere,
            WorkMoveRepos(
                files=repos.library_files,
                works=repos.works,
                song_masters=repos.song_masters,
                matches=repos.matches,
            ),
        )
        conn.commit()

        assert work_row(conn, mb_work) is not None
        assert recordings_on(conn, mb_work) == grouping_recordings
