"""Host capacity (R18, Lance 2026-10-27: re-measure on any host change, as one command): stand-in
listener sessions of a preset's chain, their frame load, and the start-up cost of a tune-in."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from processor_harness import audio_io, host_load, host_load_cli, signals
from processor_harness.host_load import FrameLoad
from processor_harness.presets import load_preset

LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
PRESETS = Path(__file__).resolve().parents[3] / "scripts" / "processor_harness" / "presets"


def test_frame_load_is_each_ticks_span_against_the_20_ms_frame() -> None:
    spans = [2.0] * 997 + [9.0, 12.0, 25.0]

    load = host_load.frame_load(spans)

    assert load == FrameLoad(
        ticks=1000, p50_ms=2.0, p999_ms=pytest.approx(12.013), max_ms=25.0, late=1
    )
    assert load.p999_share == pytest.approx(12.013 / 20)


def test_a_frame_exactly_20_ms_long_is_not_late() -> None:
    assert host_load.frame_load([20.0, 1.0]).late == 0


def test_each_session_starts_at_another_song() -> None:
    songs = ["a", "b", "c"]

    assert host_load.rotated(songs, 0) == ["a", "b", "c"]
    assert host_load.rotated(songs, 1) == ["b", "c", "a"]
    assert host_load.rotated(songs, 4) == ["b", "c", "a"]


def test_the_session_script_runs_the_presets_chain_on_the_streams_encoder(tmp_path: Path) -> None:
    preset = load_preset(PRESETS / "limiter" / "chain-r17.toml")

    script = host_load.write_session_script(preset, tmp_path / "session.liq")

    text = script.read_text(encoding="utf-8")
    assert preset.chain is not None
    assert f'%include "{preset.chain.as_posix()}"' in text
    assert '("limit_db", -2.5)' in text
    assert 'codec="libmp3lame", b="128k", ar=44100' in text
    assert "clock.assign_new" not in text  # real time, like a listener's session


@pytest.mark.slow
@pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset")
def test_one_real_session_of_bypass_runs_in_real_time_well_inside_its_frames(
    tmp_path: Path,
) -> None:
    assert LIQUIDSOAP is not None
    song = tmp_path / "tone.wav"
    audio_io.write_wav(song, signals.tone(44100, 1000.0, -20.0, 12.0).samples, 44100)
    script = host_load.write_session_script(
        load_preset(PRESETS / "bypass.toml"), tmp_path / "s.liq"
    )

    result = host_load.run_sessions(
        Path(LIQUIDSOAP), script, [song], tmp_path / "work", sessions=1, warm_s=2.0, run_s=4.0,
        cpus=(),
    )  # fmt: skip

    assert 150 <= result.loads[0].ticks <= 250  # about 4 s of 20 ms frames
    assert result.loads[0].p50_ms < 20.0
    # Windows counts process CPU in ~16 ms steps: a tone through bypass (~2% of a thread) can
    # read 0 over a few seconds, so only the upper bound says anything here.
    assert 0.0 <= result.cpu_per_session[0] < 1.0


@pytest.mark.slow
@pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset")
def test_the_command_reports_start_up_and_each_session_count(tmp_path: Path) -> None:
    assert LIQUIDSOAP is not None
    song = tmp_path / "tone.wav"
    audio_io.write_wav(song, signals.tone(44100, 1000.0, -20.0, 12.0).samples, 44100)
    corpus = tmp_path / "corpus.toml"
    corpus.write_text(
        'sample_rate = 44100\nprogramme_lufs = -18.0\n\n[[programme]]\nid = "tone"\n'
        f'path = "{song.as_posix()}"\ncategory = "music"\nstart_s = 0.0\nduration_s = 12.0\n',
        encoding="utf-8",
    )

    code = host_load_cli.main(
        [str(PRESETS / "bypass.toml"), "--corpus", str(corpus), "--out", str(tmp_path / "out"),
         "--liquidsoap", LIQUIDSOAP, "--sessions", "1", "2", "--warm", "2", "--seconds", "3",
         "--starts", "1"]
    )  # fmt: skip

    report = json.loads((tmp_path / "out" / "host_load.json").read_text(encoding="utf-8"))
    assert code == 0
    assert [row["sessions"] for row in report["concurrency"]] == [1, 2]
    assert len(report["startup"]["warm"]["cpu_s"]) == 1
    assert report["preset"] == "bypass"
