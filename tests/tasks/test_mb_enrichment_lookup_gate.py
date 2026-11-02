"""The recordings phase looks up a recording that lacks a duration or a MusicBrainz work.

PR #225 gated the per-recording lookup on `duration_ms IS NULL`, when the
length was all the phase read. The lookup also carries the performed work
and the live flag, so a recording on no MusicBrainz work is looked up too,
and what the payload says is applied; a recording that has both stays
skipped.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from tests.tasks.test_mb_enrichment_progress import _fake_connect, _make_entity

_MB_WORK = "w-mb"


def _stub_repo_factory(recordings: list[Any]) -> Any:
    def factory(_conn: Any) -> MagicMock:
        repos = MagicMock()
        repos.artists.list_unenhanced.return_value = []
        repos.works.list_needing_enhancement.return_value = []
        repos.works.list_musicbrainz_ids.return_value = {_MB_WORK}
        repos.recordings.list_needing_enhancement.return_value = recordings
        return repos

    return factory


def _recording(mbid: str, *, duration_ms: int | None, work_id: str | None) -> MagicMock:
    rec = _make_entity(mbid, name_field="title")
    rec.duration_ms = duration_ms
    rec.work_id = work_id
    return rec


def _mb_client(mock_mb_cls: MagicMock, payloads: dict[str, Any]) -> MagicMock:
    client = mock_mb_cls.return_value
    client.lookup_recording.side_effect = lambda mbid: payloads.get(mbid)
    client.live_fetches = 0
    client.cache_hits = 0
    client.__enter__ = lambda self: self
    client.__exit__ = lambda self, *exc: False
    return client


@patch("backend.tasks.mb_enrichment_tasks.apply_recording_lookup")
@patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
@patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
@patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
@patch("backend.tasks._task_run.PgSystemLogRepository")
@patch("backend.tasks._task_run.PgTaskProgressRepository")
@patch("backend.tasks.mb_enrichment_tasks.connect_sync")
@patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
def test_looks_up_what_lacks_a_duration_or_a_musicbrainz_work(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _mock_progress_cls: MagicMock,
    _sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
    mock_apply: MagicMock,
) -> None:
    complete = _recording("r-complete", duration_ms=215_000, work_id=_MB_WORK)
    on_local_work = _recording("r-local-work", duration_ms=215_000, work_id="w-local")
    on_no_work = _recording("r-no-work", duration_ms=215_000, work_id=None)
    no_duration = _recording("r-no-duration", duration_ms=None, work_id=_MB_WORK)
    pending = [complete, on_local_work, on_no_work, no_duration]

    mock_connect.side_effect = _fake_connect
    mock_factory_cls.side_effect = _stub_repo_factory(pending)
    client = _mb_client(mock_mb_cls, {"r-no-work": {"id": "r-no-work", "length": 215_000}})

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    result = mb_enrichment_task.call_local()

    looked_up = sorted(c.args[0] for c in client.lookup_recording.call_args_list)
    assert looked_up == ["r-local-work", "r-no-duration", "r-no-work"]
    # Every recording counts as done; the complete one needed no call.
    assert result["recordings_done"] == 4
    # Only the recording whose lookup answered has the payload applied.
    assert [c.args[0] for c in mock_apply.call_args_list] == [on_no_work]
    assert mock_apply.call_args_list[0].args[1] == {"id": "r-no-work", "length": 215_000}


@patch("backend.tasks.mb_enrichment_tasks.apply_recording_lookup")
@patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
@patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
@patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
@patch("backend.tasks._task_run.PgSystemLogRepository")
@patch("backend.tasks._task_run.PgTaskProgressRepository")
@patch("backend.tasks.mb_enrichment_tasks.connect_sync")
@patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
def test_asks_which_pending_works_are_musicbrainz_in_one_query(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _mock_progress_cls: MagicMock,
    _sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
    _mock_apply: MagicMock,
) -> None:
    pending = [
        _recording("r1", duration_ms=1, work_id=_MB_WORK),
        _recording("r2", duration_ms=1, work_id="w-local"),
        _recording("r3", duration_ms=1, work_id=None),
    ]
    factories: list[MagicMock] = []
    inner = _stub_repo_factory(pending)

    def factory(conn: Any) -> MagicMock:
        repos = inner(conn)
        factories.append(repos)
        return repos

    mock_connect.side_effect = _fake_connect
    mock_factory_cls.side_effect = factory
    _mb_client(mock_mb_cls, {})

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    mb_enrichment_task.call_local()

    asked = [
        sorted(c.args[0])
        for repos in factories
        for c in repos.works.list_musicbrainz_ids.call_args_list
    ]
    assert asked == [sorted([_MB_WORK, "w-local"])]
