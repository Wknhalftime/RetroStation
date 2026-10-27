"""The harness stays off the cores reserved for listener sessions (R18, Lance 2026-10-27): each
command moves itself to the other cores first, and every render, FFmpeg run and worker process it
starts inherits that."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest
from processor_harness import (
    affinity,
    calibrate_cli,
    cli,
    clipping_guard_cli,
    limiter_gr_cli,
)

from backend.playout.cpu_reservation import other_cpus, physical_cores


@pytest.fixture
def own_affinity() -> Iterator[psutil.Process]:
    me = psutil.Process()
    before = me.cpu_affinity()
    yield me
    me.cpu_affinity(before)


def test_the_harness_moves_off_the_reserved_cores(own_affinity: psutil.Process) -> None:
    cores = physical_cores()
    if len(cores) < 2:
        pytest.skip("one core: nothing to reserve")

    cpus = affinity.stay_off_stream_cores(1)

    assert cpus == other_cpus(cores, 1)
    assert tuple(own_affinity.cpu_affinity()) == cpus
    assert not set(cpus) & set(cores[-1])


def test_with_no_reserved_cores_the_harness_keeps_every_cpu(own_affinity: psutil.Process) -> None:
    cpus = affinity.stay_off_stream_cores(0)

    assert sorted(own_affinity.cpu_affinity()) == sorted(cpus) == list(range(psutil.cpu_count()))


class MovedError(Exception):
    """Raised in place of moving off the reserved cores, to see who moves and when."""


@pytest.mark.parametrize(
    "command",
    [cli.main, clipping_guard_cli.main, limiter_gr_cli.main, calibrate_cli.main],
    ids=["run_processor_harness", "run_clipping_guard", "run_limiter_gr", "calibrate"],
)
def test_each_harness_command_moves_off_the_reserved_cores_before_any_work(
    command: Callable[[list[str]], int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def moved(reserved_cores: int | None = None) -> tuple[int, ...]:
        raise MovedError

    monkeypatch.setattr(affinity, "stay_off_stream_cores", moved)

    with pytest.raises(MovedError):
        command([str(tmp_path / "nothing.toml"), str(tmp_path / "nowhere")])
