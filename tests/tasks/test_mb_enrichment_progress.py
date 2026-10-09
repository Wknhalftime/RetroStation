"""Progress-emission tests for mb_enrichment_task."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from backend.domain.enums import TaskStatus, TaskType


@pytest.fixture(autouse=True)
def _no_recheck_hand_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """mb_enrichment_task ends by queuing rematch_undecided_task("changed") (spec 2026-10-05
    §4.2). Stub it so these tests never put a re-check on the real Huey queue."""
    monkeypatch.setattr("backend.tasks.matching_recheck_tasks.rematch_undecided_task", MagicMock())
    # D15 (AUD-R026): the MB pass first queues link_local_artists_task(); stub it too.
    monkeypatch.setattr("backend.tasks.artist_linking_tasks.link_local_artists_task", MagicMock())


def _mk_conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    # DELETE orphans query returns 0 rowcount by default
    cur = MagicMock()
    cur.rowcount = 0
    conn.execute.return_value = cur
    return conn


def _fake_connect(_url: str, *, autocommit: bool = False) -> MagicMock:
    return _mk_conn()


def _stub_repo_factory(
    artists: list[Any],
    works: list[Any],
    recordings: list[Any],
) -> Any:
    """Return a factory that mints RepositoryFactory-shaped mocks.

    The pre-count connection and the three per-phase connections each construct
    their own RepositoryFactory; the same underlying entity lists are returned
    so the iteration counts match what the pre-count observed.
    """

    def factory(_conn: Any) -> MagicMock:
        repos = MagicMock()
        repos.artists.list_unenhanced.return_value = artists
        repos.works.list_needing_enhancement.return_value = works
        repos.recordings.list_needing_enhancement.return_value = recordings
        return repos

    return factory


def _make_entity(mbid: str, name_field: str = "name") -> MagicMock:
    """Build an MBID-less artist / work / recording stub (mbid=None).

    Do NOT reuse for tests exercising Tier 2/3; rebuild with a real MBID and
    explicit `disambiguation` / `sort_name` values for those cases.
    """
    ent = MagicMock()
    ent.id = mbid
    ent.mbid = None  # MBID-less: an artist is quarantined (AUD-R008)
    ent.disambiguation = None  # avoid MagicMock truthiness in Tier 2/3 checks
    ent.sort_name = f"Name-{mbid}"  # deterministic value, avoids MagicMock `in (...)` comparison
    setattr(ent, name_field, f"Name-{mbid}")
    ent.duration_ms = None
    return ent


def _mbid_entity(mbid: str, name_field: str = "name") -> MagicMock:
    """Build an MBID-known artist stub (Tier 2/3 path — mbid is set).

    Unlike `_make_entity` (mbid=None, forces the bare-artist path), this
    stub always carries an MBID so `_enhance_artist` goes straight to the
    lookup branch (the only artist path since AUD-R008).
    """
    ent = MagicMock()
    ent.id = mbid
    ent.mbid = mbid
    ent.disambiguation = None
    ent.sort_name = f"Name-{mbid}"
    setattr(ent, name_field, f"Name-{mbid}")
    ent.duration_ms = None
    return ent


def _progress_calls(mock_repo: MagicMock) -> list[Any]:
    return [call.args[0] for call in mock_repo.upsert.call_args_list]


def _system_log_calls(mock_repo: MagicMock) -> list[Any]:
    return [call.args[0] for call in mock_repo.create.call_args_list]


class TestMbEnrichmentProgress:
    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_emits_running_with_combined_total(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        artists = [_make_entity(f"a{i}") for i in range(3)]
        works = [_make_entity(f"w{i}", name_field="title") for i in range(2)]
        recordings = [_make_entity(f"r{i}", name_field="title") for i in range(4)]

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory(artists, works, recordings)
        mock_mb_cls.return_value.search_artist.return_value = []
        mock_mb_cls.return_value.lookup_artist.return_value = None
        mock_mb_cls.return_value.lookup_recording.return_value = None

        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        mb_enrichment_task.call_local()

        calls = _progress_calls(mock_progress_repo)
        assert calls[0].status == TaskStatus.RUNNING
        assert calls[0].task_type == TaskType.MB_ENRICHMENT
        assert calls[0].progress_data["total"] == 9

    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_processed_reaches_total_by_completion(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        artists = [_make_entity(f"a{i}") for i in range(2)]
        works = [_make_entity(f"w{i}", name_field="title") for i in range(1)]
        recordings = [_make_entity(f"r{i}", name_field="title") for i in range(2)]

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory(artists, works, recordings)
        mock_mb_cls.return_value.search_artist.return_value = []
        mock_mb_cls.return_value.lookup_artist.return_value = None
        mock_mb_cls.return_value.lookup_recording.return_value = None

        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        mb_enrichment_task.call_local()

        calls = _progress_calls(mock_progress_repo)
        assert calls[-1].status == TaskStatus.COMPLETED
        assert calls[-1].progress_data["processed"] == 5
        assert calls[-1].progress_data["total"] == 5

    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_phase_label_cycles_through_three_phases(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        artists = [_make_entity("a0")]
        works = [_make_entity("w0", name_field="title")]
        recordings = [_make_entity("r0", name_field="title")]

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory(artists, works, recordings)
        mock_mb_cls.return_value.search_artist.return_value = []
        mock_mb_cls.return_value.lookup_artist.return_value = None
        mock_mb_cls.return_value.lookup_recording.return_value = None

        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        mb_enrichment_task.call_local()

        running = [c for c in _progress_calls(mock_progress_repo) if c.status == TaskStatus.RUNNING]
        phases = [c.progress_data.get("phase") for c in running]
        # initial upsert phase=artists, plus one per-entity upsert each phase
        assert "artists" in phases
        assert "works" in phases
        assert "recordings" in phases
        # last running upsert should be in the recordings phase
        assert running[-1].progress_data["phase"] == "recordings"

    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_marks_failed_on_mid_run_exception(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        # Succeed on the pre-count autocommit + pre-count read, then raise.
        call_log = {"n": 0}

        def failing_connect(_url: str, *, autocommit: bool = False) -> MagicMock:
            call_log["n"] += 1
            if call_log["n"] <= 2:
                return _mk_conn()
            raise RuntimeError("phase boom")

        mock_connect.side_effect = failing_connect
        mock_factory_cls.side_effect = _stub_repo_factory([], [], [])

        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo
        mock_sys_log = MagicMock()
        mock_sys_log_cls.return_value = mock_sys_log

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        with pytest.raises(RuntimeError):
            mb_enrichment_task.call_local()

        # When pending lists are empty but connect_sync still raises on the
        # artists phase, the outer except must catch + emit FAILED.
        # (If pre-count returns nonzero, same behavior applies.)
        statuses = [c.status for c in _progress_calls(mock_progress_repo)]
        # If all pre-count lists were empty, processed == total == 0 and we
        # would have jumped straight to COMPLETED. Force at least one artist.
        # Re-setup with one entity:
        artists = [_make_entity("a0")]
        mock_factory_cls.side_effect = _stub_repo_factory(artists, [], [])
        call_log["n"] = 0
        mock_progress_repo.reset_mock()
        mock_sys_log.reset_mock()

        with pytest.raises(RuntimeError):
            mb_enrichment_task.call_local()

        statuses = [c.status for c in _progress_calls(mock_progress_repo)]
        assert TaskStatus.FAILED in statuses
        log_entries = _system_log_calls(mock_sys_log)
        failed = next(
            c for c in _progress_calls(mock_progress_repo) if c.status == TaskStatus.FAILED
        )
        assert any(entry.trace_id == failed.task_id for entry in log_entries)
        # The traceback goes with the message: a bare str(exc) such as
        # "[Errno 22] Invalid argument" says nothing about where it came from.
        failed_log = next(e for e in log_entries if e.message == "mb_enrichment_failed")
        assert "phase boom" in failed_log.details["traceback"]
        assert "mb_enrichment_tasks.py" in failed_log.details["traceback"]

    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_zero_pending_completes_cleanly(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory([], [], [])
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        mb_enrichment_task.call_local()

        statuses = [c.status for c in _progress_calls(mock_progress_repo)]
        assert statuses == [TaskStatus.RUNNING, TaskStatus.COMPLETED]
        last = _progress_calls(mock_progress_repo)[-1]
        assert last.progress_data["total"] == 0
        assert last.progress_data["processed"] == 0


# ---------------------------------------------------------------------------
# mb_task_summary schema (Step 3) — verify the nested phases dict is emitted
# with stable keys even when a phase has no MB traffic (works).
# ---------------------------------------------------------------------------


class TestMbEnrichmentSummary:
    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_summary_includes_all_three_phases_with_stable_schema(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        _mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        artists = [_make_entity(f"a{i}") for i in range(2)]
        # Make one work row MBID-bearing so `distinct_mbids` exercises the
        # dynamic computation path (falsifies any accidental hardcoded-0).
        works = [_make_entity(f"w{i}", name_field="title") for i in range(3)]
        works[0].mbid = "work-mbid-seed"
        recordings = [_make_entity(f"r{i}", name_field="title") for i in range(4)]

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory(artists, works, recordings)
        # Mock client returns nothing from MB but the counters must still be
        # readable — configure them as plain ints (MagicMock subtraction is
        # undefined otherwise).
        mock_mb_cls.return_value.search_artist.return_value = []
        mock_mb_cls.return_value.lookup_artist.return_value = None
        mock_mb_cls.return_value.lookup_recording.return_value = None
        mock_mb_cls.return_value.live_fetches = 0
        mock_mb_cls.return_value.cache_hits = 0
        mock_mb_cls.return_value.__enter__ = lambda self: self
        mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        with capture_logs() as events:
            mb_enrichment_task.call_local()

        summary_events = [e for e in events if e.get("event") == "mb_task_summary"]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert summary["task_type"] == "mb_enrichment"

        phases = summary["phases"]
        assert set(phases.keys()) == {"artists", "works", "recordings"}

        # Every phase reports the same field set, even no-MB-traffic `works`.
        required_fields = {
            "rows_queued",
            "distinct_mbids",
            "live_fetches_delta",
            "cache_hits_delta",
            "duplicate_mbid_ratio",
        }
        for name, phase in phases.items():
            assert required_fields.issubset(phase.keys()), f"{name} missing fields"

        # Row counts match the fixture.
        assert phases["artists"]["rows_queued"] == 2
        assert phases["works"]["rows_queued"] == 3
        assert phases["recordings"]["rows_queued"] == 4
        # Works phase never calls MB — only live_fetches/cache_hits deltas are
        # zero by contract. distinct_mbids is computed dynamically from the
        # fixture (one MBID-bearing work seeded above proves it).
        assert phases["works"]["live_fetches_delta"] == 0
        assert phases["works"]["cache_hits_delta"] == 0
        assert phases["works"]["distinct_mbids"] == 1
        # 1 - (1 distinct / 3 rows) = 0.666...
        assert phases["works"]["duplicate_mbid_ratio"] == pytest.approx(2 / 3)

    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_recordings_phase_404_sentinel_does_not_refetch(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        _mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        # 404-as-None in the pre-pass map is authoritative: the per-item
        # loop reads `None` and does NOT re-query. With 3 recordings all
        # returning None from MB, total calls = 3 (pre-pass), NOT 6
        # (pre-pass + per-item re-query).
        recordings = [_make_entity(f"r{i}", name_field="title") for i in range(3)]

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory([], [], recordings)
        mock_mb_cls.return_value.search_artist.return_value = []
        mock_mb_cls.return_value.lookup_artist.return_value = None
        mock_mb_cls.return_value.lookup_recording.return_value = None  # 404
        mock_mb_cls.return_value.live_fetches = 0
        mock_mb_cls.return_value.cache_hits = 0
        mock_mb_cls.return_value.__enter__ = lambda self: self
        mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        mb_enrichment_task.call_local()

        # Exactly one lookup_recording call per distinct MBID, all from the
        # pre-pass. The per-item loop sees `None` in the map and does NOT
        # re-query (distinct from key-absent which WOULD re-query).
        assert mock_mb_cls.return_value.lookup_recording.call_count == 3


# ---------------------------------------------------------------------------
# Artist phase outcome counting (AUD-R008 gate 2 mutation-kill tests) — the
# per-item loop in `_run_artist_phase` distinguishes ENHANCED (ctx.done)
# from FAILED (ctx.failed) and isolates a per-item retriable exception to
# just that row. These use MBID-known (Tier 2/3) artists, the only path
# that enhances an artist since AUD-R008.
# ---------------------------------------------------------------------------


class TestArtistPhaseOutcomeCounting:
    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_enhanced_and_404_failed_artists_are_counted_separately(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        _mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        good = _mbid_entity("mbid-good")
        bad = _mbid_entity("mbid-404")
        artists = [good, bad]

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory(artists, [], [])
        mock_mb_cls.return_value.lookup_artist.side_effect = lambda mbid: (
            {"id": mbid, "name": "X", "sort-name": "X, Sorted"} if mbid == "mbid-good" else None
        )
        mock_mb_cls.return_value.live_fetches = 0
        mock_mb_cls.return_value.cache_hits = 0
        mock_mb_cls.return_value.__enter__ = lambda self: self
        mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        mb_enrichment_task.call_local()

        last = _progress_calls(_mock_progress_cls.return_value)[-1]
        assert last.progress_data["artists_done"] == 1
        assert last.progress_data["artists_failed"] == 1

    @patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks._task_run.PgSystemLogRepository")
    @patch("backend.tasks._task_run.PgTaskProgressRepository")
    @patch("backend.tasks.mb_enrichment_tasks.connect_sync")
    @patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())
    def test_retriable_exception_is_isolated_to_one_artist(
        self,
        _task_run_connect: MagicMock,
        mock_connect: MagicMock,
        _mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        mock_factory_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_mb_cls: MagicMock,
    ) -> None:
        """A transient httpx failure on one artist rolls back only that row.

        `lookup_artist("mbid-flaky")` always raises: once from the pre-pass
        coalesce (swallowed, MBID omitted from the map) and once from the
        per-item fallback lookup inside `_enhance_artist` (NOT swallowed —
        propagates to `_run_artist_phase`'s `_PER_ITEM_RETRIABLE_ERRORS`
        handler). The other artist must still succeed.
        """
        import httpx

        good = _mbid_entity("mbid-ok")
        flaky = _mbid_entity("mbid-flaky")
        artists = [good, flaky]

        def _lookup_artist(mbid: str) -> dict[str, Any] | None:
            if mbid == "mbid-flaky":
                raise httpx.ConnectError("simulated transient failure")
            return {"id": mbid, "name": "X", "sort-name": "X, Sorted"}

        mock_connect.side_effect = _fake_connect
        mock_factory_cls.side_effect = _stub_repo_factory(artists, [], [])
        mock_mb_cls.return_value.lookup_artist.side_effect = _lookup_artist
        mock_mb_cls.return_value.live_fetches = 0
        mock_mb_cls.return_value.cache_hits = 0
        mock_mb_cls.return_value.__enter__ = lambda self: self
        mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

        from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

        with capture_logs() as events:
            mb_enrichment_task.call_local()

        last = _progress_calls(_mock_progress_cls.return_value)[-1]
        assert last.progress_data["artists_done"] == 1
        assert last.progress_data["artists_failed"] == 1

        failure_events = [
            e
            for e in events
            if e.get("event") == "mb_artist_enhancement_failed"
            and e.get("reason") == "per_item_exception"
        ]
        assert len(failure_events) == 1
        assert failure_events[0]["artist_id"] == "mbid-flaky"
