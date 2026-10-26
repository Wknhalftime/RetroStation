"""The acompressor model (brief math item 3a) against FFmpeg itself: the model must reproduce the
filter's output sample for sample, in every detection and link mode."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import acompressor
from processor_harness.metrics.acompressor import Settings

FS = 44100
FFMPEG = shutil.which("ffmpeg")
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")


def program() -> np.ndarray:
    """Noise bursts with a level step, uneven between channels, so link modes differ."""
    loud = signals.noise_with_pauses(FS, burst_s=1.0, pause_s=0.3, bursts=1, level_dbfs=-12.0)
    soft = signals.noise_with_pauses(FS, burst_s=1.0, pause_s=0.3, bursts=1, level_dbfs=-30.0)
    x = np.concatenate([soft.samples, loud.samples, soft.samples])
    x[:, 1] *= 0.5
    return x


def test_settings_convert_db_to_ffmpegs_linear_values() -> None:
    s = Settings.from_db(
        threshold_db=-20.0, ratio=4.0, knee_db=9.03, makeup_db=6.0, attack_ms=20.0, release_ms=250.0
    )

    assert s.threshold == pytest.approx(0.1)
    assert s.knee == pytest.approx(2.8284, rel=1e-3)
    assert s.makeup == pytest.approx(1.9953, rel=1e-3)


def test_below_the_knee_the_gain_is_unity() -> None:
    # Peak detection, so the level is an amplitude (in RMS mode it would be a power).
    s = Settings.from_db(-20.0, 4.0, 9.03, 0.0, 20.0, 250.0, rms=False)
    quiet = np.full(1000, 10 ** (-40 / 20))

    np.testing.assert_allclose(acompressor.gain(quiet, s), 1.0)


def test_well_above_the_knee_the_level_follows_the_ratio() -> None:
    # Peak detection, a steady level 20 dB over threshold: 20 / 4 = 5 dB over, so 15 dB of gain
    # reduction.
    s = Settings.from_db(-20.0, 4.0, 9.03, 0.0, 20.0, 250.0, rms=False)
    level = np.full(10, 10 ** (0 / 20))

    assert 20 * np.log10(acompressor.gain(level, s)[0]) == pytest.approx(-15.0, abs=1e-9)


def test_time_constant_is_a_quarter_of_the_set_value() -> None:
    # A unit step into the detector: after N = t_ms * fs / 4000 samples it reaches 1 - 1/e.
    d = np.ones((4410, 1))

    level = acompressor.detector(d, FS, attack_ms=40.0, release_ms=400.0, rms=False, link_max=False)

    n = round(40.0 * FS / 4000)
    assert level[n - 1] == pytest.approx(1 - np.exp(-1), abs=0.01)


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
@pytest.mark.parametrize(("rms", "link_max"), [(True, False), (False, False), (False, True)])
def test_model_matches_ffmpeg_sample_for_sample(tmp_path: Path, rms: bool, link_max: bool) -> None:
    assert FFMPEG is not None
    x = program()
    audio_io.write_wav(tmp_path / "in.wav", x, FS)
    s = Settings.from_db(-24.0, 4.0, 9.03, 6.0, 20.0, 250.0, rms=rms, link_max=link_max)
    graph = (
        f"[0:a]acompressor=threshold={s.threshold}:ratio={s.ratio}:knee={s.knee}:"
        f"makeup={s.makeup}:attack={s.attack_ms}:release={s.release_ms}:"
        f"detection={'rms' if rms else 'peak'}:link={'maximum' if link_max else 'average'}[out]"
    )

    run = render.render_ffmpeg(Path(FFMPEG), graph, tmp_path / "in.wav", tmp_path / "out.wav")

    measured, _ = audio_io.read_wav(run.output)
    modelled = acompressor.process(audio_io.read_wav(tmp_path / "in.wav")[0], FS, s)
    assert np.max(np.abs(measured - modelled)) < 1e-6


@pytest.mark.slow
@pytest.mark.timeout(180)
@pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset")
def test_model_matches_acompressor_inside_liquidsoap(tmp_path: Path) -> None:
    # Liquidsoap links FFmpeg 7.1: the authoritative build.
    assert LIQUIDSOAP is not None
    x = program()
    audio_io.write_wav(tmp_path / "in.wav", x, FS)
    s = Settings.from_db(-24.0, 4.0, 9.03, 6.0, 20.0, 250.0)
    chain = tmp_path / "chain.liq"
    chain.write_text(
        "def chain(s) =\n"
        '  raw = ffmpeg.raw.encode.audio(%ffmpeg(%audio.raw(sample_format = "flt", '
        "sample_rate = 44100, channels = 2)), s)\n"
        "  def mkfilter(graph) =\n"
        "    a = ffmpeg.filter.audio.input(graph, source.tracks(raw).audio)\n"
        f"    a = ffmpeg.filter.acompressor(graph, threshold={s.threshold}, ratio={s.ratio}, "
        f"knee={s.knee}, makeup={s.makeup}, attack={s.attack_ms}, release={s.release_ms}, a)\n"
        "    source({audio=ffmpeg.filter.audio.output(graph, a)})\n"
        "  end\n"
        "  ffmpeg.raw.decode.audio(ffmpeg.filter.create(mkfilter))\n"
        "end\n",
        encoding="utf-8",
    )

    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), chain, {}, tmp_path / "in.wav", tmp_path / "out.wav", tmp_path / "c"
    )

    measured, _ = audio_io.read_wav(run.output)
    modelled = acompressor.process(audio_io.read_wav(tmp_path / "in.wav")[0], FS, s)
    n = min(len(measured), len(modelled))
    assert np.max(np.abs(measured[:n] - modelled[:n])) < 1e-6
