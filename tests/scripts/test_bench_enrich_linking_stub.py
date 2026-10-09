"""scripts/bench_enrich.py stubs the linking hand-off too (AUD-R026, D15).

mb_enrichment_task queues link_local_artists_task() before the re-check. A --phase both bench
run calls mb_enrichment_task in-process, so the stub must be in place, or the run queues real
linking work for the worker.
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


def test_linking_hand_off_is_stubbed_and_restored(bench_enrich: ModuleType) -> None:
    from backend.tasks import artist_linking_tasks

    original = artist_linking_tasks.link_local_artists_task
    with bench_enrich.follow_on_task(run=False):
        assert artist_linking_tasks.link_local_artists_task is not original
        assert artist_linking_tasks.link_local_artists_task() is None
    assert artist_linking_tasks.link_local_artists_task is original
