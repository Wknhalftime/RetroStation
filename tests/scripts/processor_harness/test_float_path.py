"""Headroom check (Lance, 2026-10-24): between the AGC and the limiter the multiband graph must
run in float end to end, so a level over full scale (dense masters at -14 LUFS, plus band
makeup) is carried, not clipped. Run inside Liquidsoap, whose FFmpeg builds the real graph."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals

FS = 44100
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
PROBE = Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains/float_probe.liq"
FLOAT_FORMATS = {"flt", "fltp", "dbl", "dblp"}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(180),
    pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset"),
]


@pytest.fixture(scope="module")
def probe_run(tmp_path_factory: pytest.TempPathFactory) -> render.RenderRun:
    """A 1 kHz tone at +6 dBFS through the probe: crossover, +6 dB band makeup, +6 dB volume."""
    assert LIQUIDSOAP is not None
    folder = tmp_path_factory.mktemp("float")
    tone = signals.tone(FS, 1000.0, level_dbfs=6.0, seconds=1.5)
    audio_io.write_wav(folder / "hot.wav", tone.samples, FS)
    return render.render_liquidsoap(
        Path(LIQUIDSOAP), PROBE, {}, folder / "hot.wav", folder / "out.wav", folder / "cache"
    )


def test_a_level_over_full_scale_comes_through_unclipped(probe_run: render.RenderRun) -> None:
    x, _ = audio_io.read_wav(probe_run.output)
    steady = x[round(0.2 * FS) : round(1.2 * FS)]

    # +6 dBFS in, +6 dB band makeup, +6 dB volume: +18 dBFS out if nothing clipped.
    assert 20 * np.log10(np.max(np.abs(steady))) == pytest.approx(18.06, abs=0.2)


def test_every_filter_link_runs_in_float(probe_run: render.RenderRun) -> None:
    # Only the graph's own taps count. Liquidsoap's outputs add buffers of their own after the
    # graph: flt for the WAV writer, and s32p (32-bit integer) for the MP3 encoder, as on the
    # stream. So the final limiter is the last guard before an integer stage.
    log = probe_run.output.with_suffix(".log").read_text(encoding="utf-8", errors="replace")
    formats = set(re.findall(r"\[ashowinfo_\d+ @[^\]]*\] n:\d+ .*?fmt:(\w+)", log))

    assert formats, "no ashowinfo lines in the render log"
    assert formats <= FLOAT_FORMATS
    assert "samplefmt:s32p" in log  # the MP3 encoder's input buffer, outside the graph
    inserted = [line for line in log.splitlines() if "auto-inserting" in line.lower()]
    assert not [line for line in inserted if re.search(r"s16|s32|u8", line)]
