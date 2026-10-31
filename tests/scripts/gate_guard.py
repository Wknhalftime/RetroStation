"""Run a gate tool under a guard (phase 0b SPEC, S10): ``python gate_guard.py <bench dsn>
<script> [args...]``.

Before the tool's first line runs:

- every psycopg connection (sync, async, and so the app's pool) is checked: one to any database
  but the bench one ends the run (exit 3), so a tool that reached the settings' DATABASE_URL,
  the dev DB or a private test DB, fails;
- the first import of each ``backend`` module (so ``backend.tasks`` and ``backend.main`` too) is
  checked: by then the tool must have pointed the app at the bench database (``DATABASE_URL``
  names it) and set ``STREAM_ENABLED=false`` and ``PYTEST_XDIST_WORKER=gate``, or the run ends
  (exit 4). This is stricter than S10 asks: no backend code loads before the guards are set;
- a tool that imports test code (``tests``; S7's lifespan recipe is copied, not imported) ends
  the run (exit 5).

The test runs this with ``DATABASE_URL`` naming another database, so the tool's own guards are
what keeps it on the bench one.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import runpy
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any

import psycopg


def _dbname(conninfo: str, kwargs: dict[str, Any]) -> str:
    if "dbname" in kwargs:
        return str(kwargs["dbname"])
    return str(psycopg.conninfo.conninfo_to_dict(conninfo).get("dbname", ""))


def _guard_connections(bench: str) -> None:
    sync_connect = psycopg.Connection.connect.__func__  # type: ignore[attr-defined]
    async_connect = psycopg.AsyncConnection.connect.__func__  # type: ignore[attr-defined]

    def check(conninfo: str, kwargs: dict[str, Any]) -> None:
        name = _dbname(conninfo, kwargs)
        if name != bench:
            print(f"GUARD: the tool opened database {name!r}", file=sys.stderr, flush=True)
            os._exit(3)

    def guarded_sync(cls: Any, conninfo: str = "", **kwargs: Any) -> Any:
        check(conninfo, kwargs)
        return sync_connect(cls, conninfo, **kwargs)

    async def guarded_async(cls: Any, conninfo: str = "", **kwargs: Any) -> Any:
        check(conninfo, kwargs)
        return await async_connect(cls, conninfo, **kwargs)

    psycopg.Connection.connect = classmethod(guarded_sync)  # type: ignore[method-assign,assignment]
    psycopg.AsyncConnection.connect = classmethod(guarded_async)  # type: ignore[method-assign,assignment]
    psycopg.connect = psycopg.Connection.connect  # type: ignore[assignment]


class _ImportGuard(importlib.abc.MetaPathFinder):
    """Checks the environment the first time a guarded package is imported."""

    def __init__(self, bench_dsn: str) -> None:
        self._bench_dsn = bench_dsn
        self._seen: set[str] = set()

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname == "tests" or fullname.startswith("tests."):
            print(f"GUARD: the tool imported test code ({fullname})", file=sys.stderr, flush=True)
            os._exit(5)
        if fullname.startswith("backend.") and fullname not in self._seen:
            self._seen.add(fullname)
            wanted = {"STREAM_ENABLED": "false", "PYTEST_XDIST_WORKER": "gate"}
            wrong = {k: os.environ.get(k) for k, v in wanted.items() if os.environ.get(k) != v}
            pointed = os.environ.get("DATABASE_URL", "")
            if _dbname(pointed, {}) != _dbname(self._bench_dsn, {}):
                wrong["DATABASE_URL"] = pointed
            if wrong:
                print(f"GUARD: {fullname} imported with {wrong}", file=sys.stderr, flush=True)
                os._exit(4)
        return None


def main() -> None:
    bench_dsn, script, *args = sys.argv[1:]
    bench = str(psycopg.conninfo.conninfo_to_dict(bench_dsn)["dbname"])
    _guard_connections(bench)
    sys.meta_path.insert(0, _ImportGuard(bench_dsn))
    sys.argv = [script, *args]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
