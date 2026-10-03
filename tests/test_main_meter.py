"""The lifespan's meter wiring (PR G2, Task 8; traceability T: T8.23-T8.27).

Requirements:
- C1: the meter starts from the lifespan, outside ``StreamingRuntime``: ``start_streaming`` and
  ``stop_streaming`` are unchanged, so the locked start-up tests stay unchanged; with
  streaming off there is no meter, no row and no telemetry connection (D34; D90 "while
  streaming runs");
- D94: the meter's row is written on a telemetry connection of its own, autocommit, with
  ``writer_options()`` (``synchronous_commit=off``, a 1 s statement and a 0.5 s lock bound);
- D90, D91, D95 and I5 at the composition root: the meter ``start_meter`` builds reads the
  running engines' pids, measures them with the real psutil meter, budgets half the threads
  and a quarter of the memory, stamps its row with the UTC-aware ``meter_clock`` and writes
  only through the connection it is given (audit MF1);
- M10 and PG13: at start-up the row a crash left RUNNING is ended first, before streaming
  starts, and a failure there is logged and start-up goes on (D46's spirit); at shutdown the
  meter stops (completing its row) before streaming stops.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

import backend.main as main_module
from backend.config import Settings
from backend.db.progress_writer import writer_options

LOUD = {"warning", "error", "critical"}
THREAD_GUARD_S = 10.0
"""Fails a test whose worker-thread tick never lands; never used to order anything."""


def never_connect() -> psycopg.Connection[object]:
    raise AssertionError("no connection may be opened")


async def test_with_streaming_off_no_meter_starts_and_no_connection_opens() -> None:
    # T8.23 (C1, D34): no runtime, no meter, and the telemetry connect is never called.
    assert await main_module.start_meter(None, never_connect) is None


async def test_the_leftover_row_is_ended_before_streaming_starts_and_startup_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T8.24 (C1, M10, PG13).
    order: list[str] = []
    settings = Settings(_env_file=None, stream_enabled=False)  # type: ignore[call-arg]
    runtime = object()

    def connect() -> psycopg.Connection[object]:
        order.append("connect")
        raise psycopg.OperationalError("the database is not answering")

    async def start_streaming(given: Settings) -> object:
        assert given is settings
        order.append("start_streaming")
        return runtime

    async def start_meter(given: object, meter_connect: Callable[[], object]) -> None:
        assert given is runtime and meter_connect is connect
        order.append("start_meter")

    monkeypatch.setattr(main_module, "start_streaming", start_streaming)
    monkeypatch.setattr(main_module, "start_meter", start_meter)
    with capture_logs() as logs:
        await main_module._prepare_meter(settings, connect)
    assert order == ["connect", "start_streaming", "start_meter"]
    assert [e for e in logs if e["log_level"] in LOUD]

    order.clear()
    ended: list[object] = []
    monkeypatch.setattr(main_module, "end_leftover_meter_row", ended.append)
    await main_module._prepare_meter(settings, connect)
    assert ended == [connect]
    assert order == ["start_streaming", "start_meter"]


async def test_the_meter_stops_before_streaming_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    # T8.25 (C1, PG13): the meter's row is completed while the engines it measures still run.
    order: list[tuple[str, object]] = []
    meter, runtime = object(), object()

    async def stop_meter(given: object) -> None:
        order.append(("stop_meter", given))

    async def stop_streaming(given: object) -> None:
        order.append(("stop_streaming", given))

    monkeypatch.setattr(main_module, "stop_meter", stop_meter)
    monkeypatch.setattr(main_module, "stop_streaming", stop_streaming)
    await main_module._shutdown(meter, runtime)
    assert order == [("stop_meter", meter), ("stop_streaming", runtime)]


def test_the_meter_writes_on_a_telemetry_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    # T8.26 (D94; design note 10): the lifespan's meter connection is opened as the cue task's
    # writer connection is (T7.10), so neither telemetry writer waits for the disk.
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    opened = object()

    def connect_sync(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return opened

    monkeypatch.setattr(main_module, "connect_sync", connect_sync)
    connect = main_module.meter_connect("postgresql://telemetry")
    assert calls == []
    assert connect() is opened
    assert calls == [
        (
            ("postgresql://telemetry",),
            {"autocommit": True, "connect_timeout": 2, "options": writer_options()},
        )
    ]


def param_values(params: object) -> Iterable[object]:
    if isinstance(params, dict):
        return params.values()
    if isinstance(params, (list, tuple)):
        return params
    return ()


class RecordingConn:
    """A telemetry connection: every statement's parameters, in order."""

    def __init__(self) -> None:
        self.params: list[list[object]] = []

    def execute(self, query: object, params: object = None) -> RecordingConn:
        self.params.append(list(param_values(params)))
        return self

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def row_data(params: list[object]) -> dict[str, Any]:
    for value in params:
        if isinstance(value, str) and value.startswith("{"):
            data = json.loads(value)
            if isinstance(data, dict):
                return data
        if isinstance(value, dict):
            return value
    raise AssertionError(f"no progress data in {params}")


async def test_the_meter_measures_the_running_engines_on_its_own_connection() -> None:
    # T8.27 (D90, D91, D95, I5; audit MF1): the real meter on the test's own process.
    opened: list[RecordingConn] = []

    def connect() -> RecordingConn:
        opened.append(RecordingConn())
        return opened[-1]

    runtime = SimpleNamespace(service=SimpleNamespace(engine_pids=lambda: [os.getpid()]))
    meter = await main_module.start_meter(runtime, connect)  # type: ignore[arg-type]
    assert meter is not None
    deadline = time.monotonic() + THREAD_GUARD_S
    while not any(conn.params for conn in opened):
        assert time.monotonic() < deadline, "the meter wrote nothing"
        await asyncio.sleep(0)
    await main_module.stop_meter(meter)

    statements = [params for conn in opened for params in conn.params]
    assert statements
    assert all("stream_resources" in params for params in statements)
    for params in statements:
        stamps = [v for v in params if isinstance(v, datetime)]
        assert stamps and all(s.utcoffset() == timedelta(0) for s in stamps)
    [running] = [p for p in statements if "running" in p][:1]
    data = row_data(running)
    assert data["open_streams"] == 1
    assert data["scope"] == "audio_engines"
    assert data["budget"] == {"cpu_share": 0.5, "memory_share": 0.25}
    assert "completed" in statements[-1]
