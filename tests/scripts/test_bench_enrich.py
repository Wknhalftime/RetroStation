"""Guards for scripts/bench_enrich.py that need no database.

The harness patches named functions to time them and stubs the follow-on
Huey task so an in-process run never enqueues work for the real worker.
Both break silently when the code they name is renamed, so pin them here.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def bench_enrich() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("bench_enrich")


def test_every_instrumented_hot_spot_exists(bench_enrich: ModuleType) -> None:
    # A renamed function would otherwise vanish from the timings and read
    # as "now costs nothing".
    timers = bench_enrich.Timers()
    assert bench_enrich.instrument(timers, phase="both") == []


def test_refuses_database_without_bench_in_its_name(bench_enrich: ModuleType) -> None:
    with pytest.raises(SystemExit, match="retrostation"):
        bench_enrich.require_bench_name("retrostation")
    bench_enrich.require_bench_name("retrostation_bench_enrich")


def test_follow_on_task_is_stubbed_and_restored(bench_enrich: ModuleType) -> None:
    from backend.tasks import mb_enrichment_tasks

    original = mb_enrichment_tasks.mb_enrichment_task
    with bench_enrich.follow_on_task(run=False):
        # library_enrichment_task resolves this name at call time; the stub
        # must be what it finds, or the run enqueues to the real Huey queue.
        assert mb_enrichment_tasks.mb_enrichment_task is not original
        assert mb_enrichment_tasks.mb_enrichment_task() == {}
    assert mb_enrichment_tasks.mb_enrichment_task is original


def test_reset_scope_limits_releases_when_asked(bench_enrich: ModuleType) -> None:
    unlimited = bench_enrich.reset_scope_sql(limit=None)
    limited = bench_enrich.reset_scope_sql(limit=25)
    assert "LIMIT" not in unlimited
    assert "LIMIT 25" in limited
    # Both scopes cover files whose lookups the task would actually issue.
    for sql in (unlimited, limited):
        assert "release_mbid IS NOT NULL" in sql
        assert "recording_mbid IS NOT NULL" in sql
