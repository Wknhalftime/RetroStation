"""The batch analyser against the real Liquidsoap 2.4.5 (slow; skipped without
LIQUIDSOAP_PATH or ffmpeg).

Spec: Cue pre-computation ("autocue.internal, lufs_target -18, amplify_behavior "keep",
timeout 15 s for files over 700 s"); D61 (a bad file is a failed analysis). Generated tones:
the assertions are relative (two tones 20 dB apart) and about the trimmed silence. One script
cache per worker session (review minor).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from backend.domain.streaming import CuePoints
from backend.playout.cue_analysis import ANALYSER_SCRIPT, AnalyserConfig, CueFile, analyse_batch
from backend.playout.liquidsoap_process import cache_env, session_base_env
from backend.services.streaming.autocue import autocue_points

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]


@pytest.fixture(scope="session")
def analyser_cache(liquidsoap_exe: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """cue_analysis.liq's script cache, built once per worker session (about 5 s cold)."""
    cache = tmp_path_factory.mktemp("cue-cache")
    subprocess.run(
        [str(liquidsoap_exe), "--cache-only", str(ANALYSER_SCRIPT)],
        env={**session_base_env(os.environ), **cache_env(cache)},
        check=True,
        capture_output=True,
        timeout=180,
    )
    return cache


@pytest.fixture
def analyser(liquidsoap_exe: Path, analyser_cache: Path) -> AnalyserConfig:
    return AnalyserConfig(exe=liquidsoap_exe, cache_dir=analyser_cache)


def tone(out: Path, *, amplitude: float, lead_s: float, tone_s: float, tail_s: float) -> Path:
    """A mono 440 Hz sine of ``amplitude`` from ``lead_s`` to ``lead_s + tone_s``, silent
    before and after."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    end = lead_s + tone_s
    wave = f"gte(t,{lead_s})*lt(t,{end})*{amplitude}*sin(440*2*PI*t)"
    source = f"aevalsrc='{wave}':s=44100:d={end + tail_s}"
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", source]
    subprocess.run([*command, "-c:a", "flac", str(out)], check=True, timeout=180)
    return out


def test_analysis_trims_silence_and_levels_to_minus_18(
    tmp_path: Path, analyser: AnalyserConfig
) -> None:
    """autocue.internal trims the lead and tail silence, and its gain brings each file to one
    loudness (-18 LUFS): the same shape 20 dB quieter needs 20 dB more gain."""
    loud = tone(tmp_path / "loud.flac", amplitude=0.25, lead_s=2.0, tone_s=10.0, tail_s=3.0)
    quiet = tone(tmp_path / "quiet.flac", amplitude=0.025, lead_s=2.0, tone_s=10.0, tail_s=3.0)
    batch = analyse_batch(
        [CueFile(str(loud), 15_000), CueFile(str(quiet), 15_000)],
        session_base_env(os.environ),
        analyser,
    )
    assert batch.stalled is None
    loud_points = autocue_points(batch.metadata[0])
    quiet_points = autocue_points(batch.metadata[1])
    assert isinstance(loud_points, CuePoints) and isinstance(quiet_points, CuePoints)
    assert 1_500 <= loud_points.cue_in_ms <= 2_100
    assert 11_400 <= loud_points.cue_out_ms <= 12_600
    assert quiet_points.gain_db - loud_points.gain_db == pytest.approx(20.0, abs=1.5)
    assert loud_points.gain_db < 0 < quiet_points.gain_db


def test_a_file_over_700_s_is_analysed_within_the_15_s_timeout(
    tmp_path: Path, analyser: AnalyserConfig
) -> None:
    """Spec: "timeout 15 s for files over 700 s". At the default 10 s, autocue declines an
    800 s file (spike: "Estimated processing duration is too long, autocue disabled!"). The
    process gets a generous 60 s so that only autocue's own timeout decides; its metadata
    comes back either way, empty when autocue declined."""
    long = tone(tmp_path / "long.flac", amplitude=0.25, lead_s=0.0, tone_s=800.0, tail_s=0.0)
    patient = replace(analyser, per_file_timeout_s=60.0)
    batch = analyse_batch([CueFile(str(long), 800_000)], session_base_env(os.environ), patient)
    assert batch.stalled is None
    assert isinstance(autocue_points(batch.metadata[0]), CuePoints), (
        "declined: the 15 s autocue timeout is not in force"
    )


def test_a_file_that_is_not_audio_fails_without_stopping_the_batch(
    tmp_path: Path, analyser: AnalyserConfig
) -> None:
    """D61/D52: a bad file is a failed analysis; it must not cost the batch."""
    junk = tmp_path / "junk.flac"
    junk.write_bytes(b"not audio" * 1_000)
    good = tone(tmp_path / "good.flac", amplitude=0.25, lead_s=1.0, tone_s=10.0, tail_s=1.0)
    batch = analyse_batch(
        [CueFile(str(junk), None), CueFile(str(good), 12_000)],
        session_base_env(os.environ),
        analyser,
    )
    assert batch.stalled is None
    assert not isinstance(autocue_points(batch.metadata.get(0, {})), CuePoints)
    assert isinstance(autocue_points(batch.metadata[1]), CuePoints)
