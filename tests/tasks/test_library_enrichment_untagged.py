"""Pass 4 of library_enrichment_task: untagged files under an artist with an MBID.

The pass runs after the three MBID-driven passes, one search per distinct
(artist MBID, title) pair, committing after each pair so a transient failure
costs that pair alone.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx

from backend.domain.enums import EnrichmentStatus, TaskStatus
from backend.domain.library import AudioMetadata, LibraryFile, UntaggedCandidate
from backend.services.untagged_recording_service import PairOutcome, PickOutcome, SkippedFile

_ARTIST = "f9776598-2689-41db-98d3-829f5510021c"
_OTHER_ARTIST = "00000000-0000-4000-8000-0000000000aa"


def _fake_connect(_url: str, *, autocommit: bool = False) -> Any:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    result = MagicMock()
    result.fetchall.return_value = []
    conn.execute.return_value = result
    return conn


def _candidate(title: str, artist_mbid: str = _ARTIST) -> UntaggedCandidate:
    lf = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{title}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(track_title=title, duration_ms=200_000),
    )
    return UntaggedCandidate(file=lf, artist_mbid=artist_mbid)


def _progress_calls(mock_repo: MagicMock) -> list[Any]:
    return [call.args[0] for call in mock_repo.upsert.call_args_list]


@patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
@patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
@patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
@patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
@patch("backend.tasks._task_run.PgSystemLogRepository")
@patch("backend.tasks._task_run.PgTaskProgressRepository")
@patch("backend.tasks.library_enrichment_tasks.enrich_untagged_pair")
@patch("backend.tasks.library_enrichment_tasks.connect_sync")
@patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
def test_untagged_pairs_are_searched_once_each_after_the_mbid_passes(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    mock_pass: MagicMock,
    mock_progress_cls: MagicMock,
    _sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    repo_cls: MagicMock,
    _mb_cls: MagicMock,
) -> None:
    conns: list[Any] = []

    def connect(url: str, *, autocommit: bool = False) -> Any:
        conn = _fake_connect(url, autocommit=autocommit)
        conns.append(conn)
        return conn

    mock_connect.side_effect = connect
    files = repo_cls.return_value.library_files
    files.get_pending_enrichment_with_release.return_value = []
    files.list_untagged_with_linked_artist.return_value = [
        _candidate("Right Here, Right Now [Album Version]"),
        _candidate("Right Here, Right Now"),
        _candidate("International Bright Young Thing"),
        _candidate("Right Here, Right Now", artist_mbid=_OTHER_ARTIST),
    ]
    mock_pass.side_effect = [
        PairOutcome(enriched=2, skipped=()),
        PairOutcome(
            enriched=0,
            skipped=(SkippedFile("/music/x.flac", PickOutcome.EMPTY, ()),),
        ),
        httpx.ConnectError("musicbrainz down"),
    ]
    mock_progress_repo = MagicMock()
    mock_progress_cls.return_value = mock_progress_repo

    from backend.tasks.library_enrichment_tasks import library_enrichment_task

    result = library_enrichment_task.call_local()

    pairs = [call.args[0] for call in mock_pass.call_args_list]
    assert [(p.artist_mbid, p.title, len(p.files)) for p in pairs] == [
        (_ARTIST, "right here right now", 2),
        (_ARTIST, "international bright young thing", 1),
        (_OTHER_ARTIST, "right here right now", 1),
    ]
    assert result == {"enriched": 2, "failed": 1}

    library_conn = conns[0]
    assert library_conn.commit.call_count == 2, "one commit per pair that completed"
    assert library_conn.rollback.call_count == 1, "the failed pair's work was rolled back"

    calls = _progress_calls(mock_progress_repo)
    assert calls[0].progress_data["total"] == 3, "the pairs count toward the run's total"
    running = [c for c in calls if c.status == TaskStatus.RUNNING]
    assert running[-1].progress_data["processed"] == 3
    assert running[1].progress_data["current_item"] == f"untagged:{_ARTIST}:right here right now"


@patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
@patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
@patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
@patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
@patch("backend.tasks._task_run.PgSystemLogRepository")
@patch("backend.tasks._task_run.PgTaskProgressRepository")
@patch("backend.tasks.library_enrichment_tasks.enrich_untagged_pair")
@patch("backend.tasks.library_enrichment_tasks.connect_sync")
@patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
def test_no_untagged_candidates_means_no_search(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    mock_pass: MagicMock,
    _progress_cls: MagicMock,
    _sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    repo_cls: MagicMock,
    _mb_cls: MagicMock,
) -> None:
    mock_connect.side_effect = _fake_connect
    files = repo_cls.return_value.library_files
    files.get_pending_enrichment_with_release.return_value = []
    files.list_untagged_with_linked_artist.return_value = []

    from backend.tasks.library_enrichment_tasks import library_enrichment_task

    result = library_enrichment_task.call_local()

    assert result == {"enriched": 0, "failed": 0}
    mock_pass.assert_not_called()
