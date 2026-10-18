"""Acceptance tests: where a matching stage's progress ports connect (spec 2026-10-17).

``backend.tasks._matching_run.run_ports(database_url)`` is a context manager yielding
``RunPorts(runs, pending, clock)``: the run row's repository, the pending-work count and a
monotonic clock in seconds. Every task gets its ports through it, looked up on the module at
call time (``_matching_run.run_ports(...)``, never ``from ... import run_ports``), so the task
tests' patch of ``backend.tasks._matching_run.run_ports`` is the only way in.

- every connection it opens is its own: autocommit, with a statement timeout in its startup
  options (the writes use ``writer_options()``), never the stage's transactional connection,
  so a slow or failed count can never abort the stage's matching;
- each connection it opened is closed when the block ends;
- a database it cannot reach never raises: the writes are dropped and the count is None.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import psycopg

from backend.db.progress_writer import writer_options


class _Connects:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self.conns: list[MagicMock] = []

    def __call__(self, _url: str, **kwargs: Any) -> MagicMock:
        self.kwargs.append(kwargs)
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        self.conns.append(conn)
        return conn


def test_ports_use_their_own_bounded_autocommit_connections_and_close_them() -> None:
    from backend.tasks import _matching_run

    connects = _Connects()
    with (
        patch("backend.tasks._matching_run.connect_sync", side_effect=connects),
        _matching_run.run_ports("postgresql://unused/unused") as ports,
    ):
        ports.pending.count_pending()
        ports.runs.open("run-1", total=3, stages=1)
        ports.runs.refresh("run-1", pending=2)
        ports.runs.finish_stage("run-1", pending=1, failed=False)
        assert isinstance(ports.clock(), float)

    assert connects.kwargs, "the ports opened no connection"
    for kwargs in connects.kwargs:
        assert kwargs.get("autocommit") is True
        assert "statement_timeout" in str(kwargs.get("options"))
    assert any(k.get("options") == writer_options() for k in connects.kwargs)
    for conn in connects.conns:
        assert conn.close.called or conn.__exit__.called, "a port connection was left open"


def test_ports_never_raise_for_an_unreachable_database() -> None:
    from backend.tasks import _matching_run

    with (
        patch(
            "backend.tasks._matching_run.connect_sync",
            side_effect=psycopg.OperationalError("connection refused"),
        ),
        _matching_run.run_ports("postgresql://unused/unused") as ports,
    ):
        assert ports.pending.count_pending() is None
        ports.runs.open("run-1", total=3, stages=1)
        ports.runs.refresh("run-1", pending=2)
        ports.runs.finish_stage("run-1", pending=None, failed=True)
