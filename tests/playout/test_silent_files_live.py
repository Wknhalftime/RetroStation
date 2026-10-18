"""The D111 verdict against the real Liquidsoap 2.4.5 (slow; skipped without LIQUIDSOAP_PATH
or ffmpeg).

Generated audio only (ffmpeg aevalsrc into tmp_path), never the library. Silence, digital or at
the dither level of the real silent rips (peak about -91 dBFS), measures at ebur128's floor, so
autocue's gain is exactly SILENT_GAIN_DB and autocue_points reads it as Silent. A short tone in
silence is measured and is not silent. This checks the rebuilt SILENT_STUB of
test_silent_files.py against the engine (Review Focus 1).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.playout.cue_analysis import ANALYSER_SCRIPT, AnalyserConfig, CueFile, analyse_batch
from backend.playout.liquidsoap_process import cache_env, session_base_env
from backend.services.streaming import autocue

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]


@pytest.fixture(scope="module")
def analyser(liquidsoap_exe: Path, tmp_path_factory: pytest.TempPathFactory) -> AnalyserConfig:
    """cue_analysis.liq with a script cache built once for this module (about 5 s cold)."""
    cache = tmp_path_factory.mktemp("cue-cache-silent")
    subprocess.run(
        [str(liquidsoap_exe), "--cache-only", str(ANALYSER_SCRIPT)],
        env={**session_base_env(os.environ), **cache_env(cache)},
        check=True,
        capture_output=True,
        timeout=180,
    )
    return AnalyserConfig(exe=liquidsoap_exe, cache_dir=cache)


def render(out: Path, expression: str, seconds: float) -> Path:
    """A mono 44.1 kHz FLAC of ``expression`` (an aevalsrc sample formula) lasting ``seconds``."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    source = f"aevalsrc='{expression}':s=44100:d={seconds}"
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", source]
    subprocess.run([*command, "-c:a", "flac", str(out)], check=True, timeout=180)
    return out


@pytest.mark.parametrize(
    "expression",
    ["0", "0.00003*sin(440*2*PI*t)"],
    ids=["digital-zero", "dither-level"],
)
def test_silence_reads_as_silent(tmp_path: Path, analyser: AnalyserConfig, expression: str) -> None:
    silence = render(tmp_path / "silence.flac", expression, 30.0)
    batch = analyse_batch([CueFile(str(silence), 30_000)], session_base_env(os.environ), analyser)
    assert batch.stalled is None
    metadata = batch.metadata[0]
    assert float(metadata["liq_amplify"].removesuffix(" dB")) == autocue.SILENT_GAIN_DB
    assert isinstance(autocue.autocue_points(metadata), autocue.Silent)


def test_a_short_blip_in_silence_is_not_silent(tmp_path: Path, analyser: AnalyserConfig) -> None:
    """Any audible stretch is not silent (D111): 5 s of tone inside 60 s is measured."""
    blip = render(tmp_path / "blip.flac", "gte(t,20)*lt(t,25)*0.25*sin(440*2*PI*t)", 60.0)
    batch = analyse_batch([CueFile(str(blip), 60_000)], session_base_env(os.environ), analyser)
    assert batch.stalled is None
    metadata = batch.metadata[0]
    assert float(metadata["liq_amplify"].removesuffix(" dB")) < autocue.SILENT_GAIN_DB
    assert not isinstance(autocue.autocue_points(metadata), autocue.Silent)
