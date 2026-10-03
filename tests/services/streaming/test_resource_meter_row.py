"""The audio-engine cost meter's one progress row (PR G2, Task 8; traceability R: T8.11-T8.21).

Requirements:
- D90: "The live cost meter (CPU and RAM per stream, open streams, machine totals) goes
  through the progress table ... one row, updated in place every ~2 s (pinned within 0.25 s,
  so a drift-compensated interval is fine) while streaming runs,
  never a new row per sample, so the table and System Logs are not flooded" (PG9: every 2 s);
- D95: "The cost meter counts the audio engines only ... the app's own share is not
  estimated" (``scope: "audio_engines"``); D91: the suggestion and the reference cost;
- D42: the engines of every station count;
- design note 15: the row's data, exactly;
- PG13 and M9: stopping waits for the tick in flight, then completes the row, with no RUNNING
  write after it; a row left RUNNING by a crash is completed at the next start;
- I4: a meter that dies is logged once (``stream_meter_died``) and its row completed; a
  meter read that fails with ``OSError`` never stops the sampler;
- I5: the meter's clock is UTC-aware, and a naive time is refused;
- M6: the meter is a Protocol (the fake below satisfies it under ``mypy --strict``);
- M17: each tick reads and writes in a worker thread.

No sleeps: the sampler's ``sleep`` is the locked ``GatedSleep``, released step by step; a
tick runs in a real worker thread, so the test waits on its outcome (a bounded wait on a
condition, never on time).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.stream_capacity import CapacityBudget
from backend.domain.streaming import InvalidStreamValueError
from backend.domain.system import TaskProgress
from backend.playout.process_meter import MachineUsage, ProcessUsage
from backend.services.streaming.resource_meter import (
    MeterPorts,
    MeterRuntime,
    ResourceMeter,
    end_leftover_meter_row_with,
    meter_row,
    sample_engines,
    start_meter_task,
    stop_meter,
)
from tests.fakes.task_progress import FakeTaskProgressRepository
from tests.services.streaming.events_rig import GatedSleep
from tests.services.streaming.helpers import Clock, make_rig
from tests.services.streaming.schedule import DAY, STATION, song

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
MACHINE = MachineUsage(
    threads=16, memory_total_mb=65_430.0, memory_available_mb=40_100.0, cpu_percent=12.0
)
METER_ID = "stream_resources"
INFO_OR_ABOVE = {"info", "warning", "error", "critical"}
THREAD_GUARD_S = 10.0
"""Fails a test whose worker-thread tick never lands; never used to order anything (a passing
run never waits on it). Wider than the locked rig's 2 s, for a loaded machine (audit note 10)."""


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds; fail after the hang guard."""
    deadline = time.monotonic() + THREAD_GUARD_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition was not reached")
        await asyncio.sleep(0)


@dataclass
class FakeMeter:
    """A ``ResourceMeter``: each pid's (cpu %, MB), the machine, failures to raise in turn,
    and an optional hold that keeps a reading in flight."""

    readings: dict[int, tuple[float | None, float]] = field(default_factory=dict)
    usage: MachineUsage = MACHINE
    failures: list[BaseException] = field(default_factory=list)
    hold: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    asked: list[list[int]] = field(default_factory=list)

    def read(self, pids: Sequence[int]) -> list[ProcessUsage]:
        self.asked.append(list(pids))
        self.entered.set()
        if self.hold is not None:
            self.hold.wait(THREAD_GUARD_S)
        if self.failures:
            raise self.failures.pop(0)
        return [ProcessUsage(pid, *self.readings[pid]) for pid in pids if pid in self.readings]

    def machine(self) -> MachineUsage:
        return self.usage


@dataclass
class RecordingWriter:
    """The meter's row writer: every row in the order it landed, from any thread."""

    rows: list[TaskProgress] = field(default_factory=list)
    completed: list[TaskProgress] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, task: TaskProgress) -> None:
        with self._lock:
            self.rows.append(task)

    def complete(self, task: TaskProgress) -> None:
        with self._lock:
            self.rows.append(task)
            self.completed.append(task)

    def statuses(self) -> list[TaskStatus]:
        with self._lock:
            return [row.status for row in self.rows]


@dataclass
class MeterRig:
    meter: FakeMeter = field(default_factory=FakeMeter)
    writer: RecordingWriter = field(default_factory=RecordingWriter)
    clock: Clock = field(default_factory=lambda: Clock(T0))
    pids: list[int] = field(default_factory=list)
    sleep: GatedSleep = field(init=False)

    def __post_init__(self) -> None:
        self.sleep = GatedSleep(self.clock)

    def start(self) -> MeterRuntime:
        meter: ResourceMeter = self.meter
        ports = MeterPorts(
            pids=lambda: list(self.pids), meter=meter, writer=self.writer, clock=self.clock
        )
        return start_meter_task(ports, self.sleep)

    async def ticks(self, count: int) -> None:
        """Let ``count`` ticks run; the sampler is then in its ``count``-th sleep."""
        for done in range(1, count + 1):
            await until(lambda done=done: len(self.sleep.asked) == done and self.sleep.pending == 1)
            if done < count:
                self.sleep.release()


def running(rows: list[TaskProgress]) -> list[TaskProgress]:
    return [row for row in rows if row.status == TaskStatus.RUNNING]


async def test_every_sample_updates_the_one_meter_row_in_place() -> None:
    # T8.11 (D90, PG9, D42): one fixed row, rewritten every 2 s with a fresh updated_at;
    # the engines of two stations are both open streams.
    rig = MeterRig(pids=[101, 202], meter=FakeMeter({101: (12.0, 74.0), 202: (14.0, 76.0)}))
    runtime = rig.start()
    await rig.ticks(3)
    rows = list(rig.writer.rows)
    await stop_meter(runtime)
    assert len(rows) == 3
    assert {(row.task_id, row.task_type, row.status) for row in rows} == {
        (METER_ID, TaskType.STREAM_RESOURCES, TaskStatus.RUNNING)
    }
    assert TaskType.STREAM_RESOURCES.value == METER_ID
    assert rig.sleep.asked == pytest.approx([2.0] * 3, abs=0.25)
    stamps = [row.updated_at for row in rows]
    assert stamps[0] == T0
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip(stamps, stamps[1:], strict=False)
    ]
    assert gaps == pytest.approx([2.0, 2.0], abs=0.25)
    assert {row.started_at for row in rows} == {T0}
    assert [row.progress_data["open_streams"] for row in rows] == [2, 2, 2]
    assert rig.meter.asked[0] == [101, 202]


async def test_the_cost_is_the_mean_of_measured_engines() -> None:
    # T8.12 (D91, D95): the mean of the engines measured; 0.5 x 16 x 100 / 13 = 61.5.
    rig = MeterRig(pids=[101, 202], meter=FakeMeter({101: (12.0, 74.0), 202: (14.0, 76.0)}))
    runtime = rig.start()
    await rig.ticks(1)
    await stop_meter(runtime)
    data = rig.writer.rows[0].progress_data
    assert data["cost"] == {
        "cpu_percent": pytest.approx(13.0),
        "memory_mb": pytest.approx(75.0),
        "source": "measured",
    }
    assert (data["suggested_max"], data["limited_by"]) == (61, "processor")
    assert data["streams"] == [
        {"cpu_percent": 12.0, "memory_mb": 74.0, "warming": False},
        {"cpu_percent": 14.0, "memory_mb": 76.0, "warming": False},
    ]


async def test_with_nobody_listening_the_reference_cost_is_used() -> None:
    # T8.13 (D91; the PR A gate): no engine, or none measured yet, costs the gate's figures.
    rig = MeterRig()
    runtime = rig.start()
    await rig.ticks(1)
    rig.pids.append(303)
    rig.meter.readings[303] = (None, 70.0)
    rig.sleep.release()
    await until(lambda: len(rig.writer.rows) == 2 and rig.sleep.pending == 1)
    await stop_meter(runtime)
    idle, warming = (row.progress_data for row in rig.writer.rows[:2])
    reference = {"cpu_percent": 13.8, "memory_mb": 75.0, "source": "reference"}
    assert (idle["open_streams"], idle["streams"], idle["cost"]) == (0, [], reference)
    assert (idle["suggested_max"], idle["limited_by"]) == (57, "processor")
    assert warming["open_streams"] == 1
    assert warming["streams"] == [{"cpu_percent": None, "memory_mb": 70.0, "warming": True}]
    assert warming["cost"] == reference


async def test_the_row_carries_the_audio_engines_the_machine_and_the_suggestion() -> None:
    # T8.14 (D90, D95, design note 15): exactly these keys; the engines only; it is a meter,
    # not a task, so it has no processed or total (the bottom bar never reads it as one).
    rig = MeterRig(pids=[101], meter=FakeMeter({101: (13.1, 74.2)}))
    runtime = rig.start()
    await rig.ticks(1)
    await stop_meter(runtime)
    data = rig.writer.rows[0].progress_data
    assert set(data) == {
        "open_streams",
        "scope",
        "streams",
        "cost",
        "machine",
        "suggested_max",
        "limited_by",
        "budget",
    }
    assert data["scope"] == "audio_engines"
    assert set(data["streams"][0]) == {"cpu_percent", "memory_mb", "warming"}
    assert set(data["cost"]) == {"cpu_percent", "memory_mb", "source"}
    assert data["machine"] == {
        "threads": 16,
        "memory_total_mb": 65_430.0,
        "memory_available_mb": 40_100.0,
        "cpu_percent": 12.0,
    }
    assert data["budget"] == {"cpu_share": 0.5, "memory_share": 0.25}
    assert "processed" not in data and "total" not in data


async def test_a_meter_failure_never_stops_the_sampler() -> None:
    # T8.15 (error handling; I4; D90 not flooded): two failed reads in a row are one warning;
    # the next tick writes the row.
    rig = MeterRig(pids=[101], meter=FakeMeter({101: (13.0, 75.0)}))
    rig.meter.failures = [OSError("access denied"), OSError("access denied")]
    with capture_logs() as logs:
        runtime = rig.start()
        await rig.ticks(3)
    await stop_meter(runtime)
    assert len(running(rig.writer.rows)) == 1
    assert rig.sleep.asked == pytest.approx([2.0] * 3, abs=0.25)
    assert len([e for e in logs if e["log_level"] in {"warning", "error", "critical"}]) == 1


async def test_the_service_lists_only_running_engines(tmp_path: Path) -> None:
    # T8.16 (D5, D6): one engine process per open stream; a closed stream's engine is gone.
    rig = make_rig(tmp_path)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    first = await rig.open()
    await rig.open()
    assert sorted(rig.service.engine_pids()) == [40_001, 40_002]
    rig.service.close(first)
    assert rig.service.engine_pids() == [40_002]


async def test_stopping_waits_for_the_tick_then_completes_the_row() -> None:
    # T8.17 (PG13, M9): a tick held in flight, then stop: its RUNNING write lands first, the
    # COMPLETED row last, and nothing RUNNING after it.
    rig = MeterRig(pids=[101], meter=FakeMeter({101: (13.0, 75.0)}, hold=threading.Event()))
    runtime = rig.start()
    await until(rig.meter.entered.is_set)
    stopping = asyncio.ensure_future(stop_meter(runtime))
    for _ in range(20):
        await asyncio.sleep(0)
    assert rig.meter.hold is not None
    rig.meter.hold.set()
    await asyncio.wait_for(stopping, THREAD_GUARD_S)
    await until(lambda: TaskStatus.RUNNING in rig.writer.statuses())
    statuses = rig.writer.statuses()
    assert statuses[-1] == TaskStatus.COMPLETED
    assert TaskStatus.RUNNING not in statuses[statuses.index(TaskStatus.COMPLETED) :]
    assert runtime.task.done()


@pytest.mark.parametrize("left", ["running", "none", "completed"])
async def test_a_running_meter_row_left_by_a_crash_is_completed(left: str) -> None:
    # T8.18 (PG13): at the next start a RUNNING meter row is completed, so the reaper never
    # flips it to failed; no row, or a completed one, is left alone. Other rows are not touched.
    repo = FakeTaskProgressRepository()
    hour_ago = T0 - timedelta(hours=1)
    scan = TaskProgress("scan-1", TaskType.SCAN, TaskStatus.RUNNING, {}, hour_ago, hour_ago)
    repo.upsert(scan)
    if left != "none":
        status = TaskStatus.RUNNING if left == "running" else TaskStatus.COMPLETED
        completed_at = None if left == "running" else hour_ago
        repo.upsert(
            TaskProgress(
                METER_ID,
                TaskType.STREAM_RESOURCES,
                status,
                {"open_streams": 1},
                hour_ago,
                hour_ago,
                completed_at,
            )
        )
    before = len(repo.received_upserts)
    end_leftover_meter_row_with(repo, T0)
    after = repo.received_upserts[before:]
    if left == "running":
        [ended] = after
        assert (ended.task_id, ended.status, ended.completed_at) == (
            METER_ID,
            TaskStatus.COMPLETED,
            T0,
        )
    else:
        assert after == []
    assert repo.get_by_id("scan-1") == scan


async def test_sampling_logs_nothing() -> None:
    # T8.19 (D90: System Logs not flooded), guard: no line per sample.
    rig = MeterRig(pids=[101], meter=FakeMeter({101: (13.0, 75.0)}))
    with capture_logs() as logs:
        runtime = rig.start()
        await rig.ticks(3)
    await stop_meter(runtime)
    assert len(rig.writer.rows) >= 3
    assert [e["event"] for e in logs if e["log_level"] in INFO_OR_ABOVE] == []


async def test_the_meter_clock_is_utc_aware_and_a_naive_time_is_refused() -> None:
    # T8.20 (I5): the production clock is UTC-aware; a naive time would read hours old to
    # the reaper's now(), so the row refuses it.
    from backend.main import meter_clock

    now = meter_clock()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    sample = sample_engines([101], FakeMeter({101: (13.0, 75.0)}), CapacityBudget())
    assert meter_row(sample, T0, T0).updated_at == T0
    with pytest.raises(InvalidStreamValueError):
        meter_row(sample, T0, datetime(2026, 10, 2, 9, 0))


async def test_a_meter_that_dies_is_logged_once_and_its_row_completed() -> None:
    # T8.21 (I4): an unexpected error ends the sampler; it is logged once and the row is
    # completed, so it never ages into a stuck "failed" task.
    rig = MeterRig(pids=[101], meter=FakeMeter({101: (13.0, 75.0)}))
    rig.meter.failures = [RuntimeError("psutil broke")]
    with capture_logs() as logs:
        runtime = rig.start()
        await asyncio.wait({runtime.task}, timeout=THREAD_GUARD_S)
        await until(lambda: bool(rig.writer.completed))
    assert runtime.task.done() and not runtime.task.cancelled()
    died = [e for e in logs if e["event"] == "stream_meter_died"]
    assert [e["log_level"] for e in died] == ["error"]
    assert rig.writer.completed[-1].status == TaskStatus.COMPLETED
    assert running(rig.writer.rows) == []
