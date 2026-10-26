"""The limiter's gain reduction, from two renders of one input: the chain with the limiter and
the same chain without it (Lance, 2026-10-26: least p95 gain reduction wins)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, limiter_gr, limiter_gr_cli, signals

FS = 44100


def dipped(x: np.ndarray, every_s: float, dip_db: float) -> np.ndarray:
    """x turned down by dip_db for the first 10% of every every_s seconds."""
    t = np.arange(len(x)) / FS
    gain = np.where((t % every_s) < 0.1 * every_s, 10 ** (-dip_db / 20), 1.0)
    return np.asarray(x * gain[:, np.newaxis])


def test_gain_reduction_is_the_reference_level_over_the_limited_one() -> None:
    ref = signals.noise_with_pauses(FS, 10.0, 0.0, 1, -12.0).samples

    gr = limiter_gr.gain_reduction(ref, dipped(ref, 1.0, 3.0), FS)

    assert gr.p50_db == pytest.approx(0.0, abs=0.01)
    assert gr.p95_db == pytest.approx(3.0, abs=0.05)
    assert gr.max_db == pytest.approx(3.0, abs=0.05)
    assert gr.active_pct == pytest.approx(10.0, abs=1.0)


def test_renders_a_few_samples_apart_are_lined_up_first() -> None:
    ref = signals.noise_with_pauses(FS, 10.0, 0.0, 1, -12.0).samples
    late = np.concatenate([np.zeros((37, 2)), dipped(ref, 1.0, 3.0)])[: len(ref)]

    gr = limiter_gr.gain_reduction(ref, late, FS)

    assert gr.p95_db == pytest.approx(3.0, abs=0.1)
    assert gr.p50_db == pytest.approx(0.0, abs=0.05)


def test_runs_are_compared_input_by_input(tmp_path: Path) -> None:
    ref = signals.noise_with_pauses(FS, 6.0, 0.0, 1, -12.0).samples
    for name, x in (("reference", ref), ("variant", dipped(ref, 1.0, 2.0))):
        (tmp_path / name).mkdir()
        audio_io.write_wav(tmp_path / name / "song.wav", x, FS)
        audio_io.write_wav(tmp_path / name / "_startup.wav", x[:100], FS)

    rows = limiter_gr.compare_runs(tmp_path / "reference", tmp_path / "variant")

    assert set(rows) == {"song"}
    assert rows["song"]["lim_gr_p95_db"] == pytest.approx(2.0, abs=0.05)


def test_the_command_writes_each_variants_gain_reduction_beside_its_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = signals.noise_with_pauses(FS, 6.0, 0.0, 1, -12.0).samples
    for name, x in (("lim-off-v1", ref), ("lim-a5-v1", dipped(ref, 1.0, 2.0))):
        (tmp_path / name).mkdir()
        audio_io.write_wav(tmp_path / name / "song.wav", x, FS)

    code = limiter_gr_cli.main(
        ["--reference", str(tmp_path / "lim-off-v1"), str(tmp_path / "lim-a5-v1")]
    )

    assert code == 0
    written = json.loads((tmp_path / "lim-a5-v1.limiter_gr.json").read_text(encoding="utf-8"))
    assert written["song"]["lim_gr_p95_db"] == pytest.approx(2.0, abs=0.05)
    assert "lim-a5-v1" in capsys.readouterr().out
