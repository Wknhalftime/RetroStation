"""The recordings phase skips the lookup of a recording that has nothing left to learn.

The batched recording search that creates a recording already stores its
length. A 2026-11-02 profile of the dev DB found 10,936 of 11,166 recording
lookups returned the length already held, so a recording with a duration that
also sits on a MusicBrainz work (the other thing a lookup carries, see
test_mb_enrichment_lookup_gate.py) is marked enhanced without a MusicBrainz
call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from tests.tasks.test_mb_enrichment_progress import (
    _fake_connect,
    _make_entity,
    _stub_repo_factory,
)


@patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
@patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
@patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
@patch("backend.tasks._task_run.PgSystemLogRepository")
@patch("backend.tasks._task_run.PgTaskProgressRepository")
@patch("backend.tasks.mb_enrichment_tasks.connect_sync")
@patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
def test_recordings_phase_skips_lookup_when_duration_known(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _mock_progress_cls: MagicMock,
    _sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
) -> None:
    known = _make_entity("r-known", name_field="title")
    known.duration_ms = 215_000
    known.work_id = "w-mb"
    missing = _make_entity("r-missing", name_field="title")
    missing.work_id = "w-mb"

    def factory(conn: Any) -> MagicMock:
        repos: MagicMock = _stub_repo_factory([], [], [known, missing])(conn)
        repos.works.list_musicbrainz_ids.return_value = {"w-mb"}
        return repos

    mock_connect.side_effect = _fake_connect
    mock_factory_cls.side_effect = factory
    mock_mb_cls.return_value.lookup_recording.return_value = None
    mock_mb_cls.return_value.live_fetches = 0
    mock_mb_cls.return_value.cache_hits = 0
    mock_mb_cls.return_value.__enter__ = lambda self: self
    mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    result = mb_enrichment_task.call_local()

    lookup = mock_mb_cls.return_value.lookup_recording
    assert [c.args[0] for c in lookup.call_args_list] == ["r-missing"]
    # Both recordings still count as done: the known one needed no call.
    assert result["recordings_done"] == 2
