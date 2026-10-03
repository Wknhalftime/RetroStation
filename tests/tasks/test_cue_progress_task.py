"""The cue task's progress wiring (PR G2, Task 7; traceability O: T7.10, T7.11).

Requirements:
- D89 and D77a: the cue run's progress goes through ``progress_tracking``; the cue worker's
  composition root wires a real progress writer and coverage read into the run;
- D94 and design note 10: the writer's connection is its own, autocommit, with
  ``writer_options()`` (``synchronous_commit=off``, a 1 s statement and a 0.5 s lock bound),
  and it is closed when the run ends;
- I7: the count is read on its own short-lived, bounded, read-only connection, never the
  writer's and never the run's: ``coverage_options()`` (10 s statement, 2 s lock, read-only),
  autocommit, closed after each read; a read that succeeds logs nothing, and one that fails
  gives None and is logged once;
- D51: nothing runs, and so nothing is built, without ``LIQUIDSOAP_PATH``;
- M13: the test reads the public seam ``cue_progress_ports`` rather than private state.
"""

from __future__ import annotations

import re
from collections import defaultdict
from contextlib import nullcontext
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

import backend.tasks.stream_cue_tasks as tasks_module
from backend.db.progress_writer import (
    ProgressWriter,
    bounded_coverage_read,
    coverage_options,
    writer_options,
)
from backend.domain.streaming import CueCoverage
from backend.services.streaming.cue_precompute import CueRunConfig, CueRunPorts
from backend.tasks.stream_cue_tasks import FailureMemory

URL = "postgresql://unused"
T0 = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
LOUD = {"warning", "error", "critical"}
INFO_OR_ABOVE = {"info", *LOUD}


def settings_in(options: str) -> dict[str, str]:
    return dict(re.findall(r"-c\s*([a-z_]+)\s*=\s*(\S+)", options))


class RunConn:
    """The run's own connection (autocommit off), as the locked E1 task tests fake it."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class StatementConn:
    """A connection that records what it is asked and answers a zero row."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.closed = False

    def execute(self, query: object, params: object = None) -> StatementConn:
        self.statements.append((str(query), params))
        return self

    def fetchone(self) -> defaultdict[object, int]:
        return defaultdict(int)

    def fetchall(self) -> list[defaultdict[object, int]]:
        return [defaultdict(int)]

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> StatementConn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Connects:
    """A recording ``connect_sync``: the run's connection, or a statement connection."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.run = RunConn()
        self.opened: list[StatementConn] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        if not kwargs.get("autocommit"):
            return nullcontext(self.run)
        conn = StatementConn()
        self.opened.append(conn)
        return conn


def configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, liquidsoap: bool = True
) -> Connects:
    connects = Connects()
    configured = SimpleNamespace(
        database_url=URL,
        stream_enabled=False,
        liquidsoap_path=tmp_path / "liquidsoap.exe" if liquidsoap else None,
        stream_work_dir=tmp_path / "stream",
    )
    monkeypatch.setattr(tasks_module, "get_settings", lambda: configured)
    monkeypatch.setattr(tasks_module, "connect_sync", connects)
    monkeypatch.setattr(tasks_module, "local_today", lambda: date(2026, 3, 14))
    monkeypatch.setattr(tasks_module, "utc_now", lambda: T0)
    monkeypatch.setattr(tasks_module, "FAILURES", FailureMemory())
    return connects


def test_the_task_wires_its_writer_and_its_coverage_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T7.10 (design note 10, D94, D89, D51; M13).
    connects = configure(monkeypatch, tmp_path)
    coverage_urls: list[str] = []
    counted: list[int] = []

    def coverage() -> CueCoverage:
        counted.append(1)
        return CueCoverage(analysable=4, ready=1, failed=0, unhashed=0)

    def fake_bounded_read(url: str) -> object:
        coverage_urls.append(url)
        return coverage

    seams: list[tuple[object, object, object]] = []
    real_ports = tasks_module.cue_progress_ports

    def spy(writer: Any, coverage_read: Any, run_id: Any) -> Any:
        seams.append((writer, coverage_read, run_id))
        return real_ports(writer, coverage_read, run_id)

    def fake_run(ports: CueRunPorts, config: CueRunConfig) -> None:
        # What a run that stored one batch does with its progress sink.
        ports.progress.batch_stored(1)
        ports.progress.run_ended(failed=False)

    monkeypatch.setattr(tasks_module, "bounded_coverage_read", fake_bounded_read)
    monkeypatch.setattr(tasks_module, "cue_progress_ports", spy)
    monkeypatch.setattr(tasks_module, "run_cue_analysis", fake_run)
    tasks_module.stream_cue_analysis_task.call_local()

    [(writer, coverage_read, _)] = seams
    assert isinstance(writer, ProgressWriter)
    assert coverage_read is coverage
    assert coverage_urls == [URL]
    assert counted == [1]  # the run's sink counted through the bounded read, not the run's conn
    writer_calls = [(a, k) for a, k in connects.calls if k.get("autocommit")]
    assert writer_calls
    for args, kwargs in writer_calls:
        assert args == (URL,)
        assert kwargs == {"autocommit": True, "connect_timeout": 2, "options": writer_options()}
    written = [params for conn in connects.opened for _, params in conn.statements]
    assert any("cue_analysis" in str(params) for params in written)
    run_calls = [k for _, k in connects.calls if not k.get("autocommit")]
    assert run_calls == [{"autocommit": False}]
    # Design note 10 (audit SF2): the run closes its writer's connection when it ends.
    assert connects.opened and all(conn.closed for conn in connects.opened)

    # D51: without LIQUIDSOAP_PATH nothing is built and nothing connects.
    connects = configure(monkeypatch, tmp_path, liquidsoap=False)
    seams.clear()
    coverage_urls.clear()
    tasks_module.stream_cue_analysis_task.call_local()
    assert (seams, coverage_urls, connects.calls) == ([], [], [])


def test_the_count_is_read_on_its_own_bounded_read_only_connection() -> None:
    # T7.11 (I7): a new connection per read, autocommit, bounded and read-only, closed after.
    options = settings_in(coverage_options())
    assert options["statement_timeout"] == "10000"
    assert options["lock_timeout"] == "2000"
    assert options["default_transaction_read_only"] == "on"

    connects = Connects()
    read = bounded_coverage_read(URL, connect=connects)
    with capture_logs() as quiet:
        assert read() == read() == CueCoverage(analysable=0, ready=0, failed=0, unhashed=0)
    # A count that succeeds says nothing in System Logs (D59, D90; audit note 4).
    assert [e for e in quiet if e["log_level"] in INFO_OR_ABOVE] == []
    assert len(connects.calls) == len(connects.opened) == 2
    for args, kwargs in connects.calls:
        assert args == (URL,)
        assert kwargs == {
            "autocommit": True,
            "connect_timeout": 5,
            "options": coverage_options(),
        }
    assert all(conn.closed for conn in connects.opened)
    assert writer_options() != coverage_options()

    def refused(*args: object, **kwargs: object) -> object:
        raise psycopg.OperationalError("connection refused")

    failing = bounded_coverage_read(URL, connect=refused)
    with capture_logs() as logs:
        assert failing() is None
        assert failing() is None
    assert len([e for e in logs if e["log_level"] in LOUD]) == 1
