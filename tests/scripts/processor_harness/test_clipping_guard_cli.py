"""The clipping guard's command: screen a run's renders, sweep the songs holding the worst blocks
over the encoder's states, and write the verdict beside the preset as its guard record."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from processor_harness import audio_io, clipping_guard, clipping_guard_cli, codec, signals

FS = 44100
FFMPEG = shutil.which("ffmpeg")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH"),
]


def make_run(root: Path, levels_dbfs: dict[str, float]) -> tuple[Path, Path]:
    """A chain preset and a run folder holding one render per song, noise at each level."""
    (root / "chains").mkdir()
    (root / "chains" / "chain.liq").write_text("def chain(s) = s end\n", encoding="utf-8")
    preset = root / "chain-x.toml"
    preset.write_text(
        'id = "chain-x"\nversion = 1\nbackend = "liquidsoap"\nchain = "chains/chain.liq"\n',
        encoding="utf-8",
    )
    renders = root / "run" / "chain-x-v1"
    renders.mkdir(parents=True)
    for song, level in levels_dbfs.items():
        x = signals.noise_with_pauses(FS, burst_s=1.0, pause_s=0.2, bursts=2, level_dbfs=level)
        audio_io.write_wav(renders / f"{song}.wav", x.samples, FS)
    audio_io.write_wav(renders / "_startup.wav", x.samples[:FS], FS)  # never judged
    return preset, root / "run"


def guard(preset: Path, run: Path, *extra: str) -> int:
    return clipping_guard_cli.main(
        [str(preset), str(run), "--ffmpeg", str(FFMPEG), "--lead-ins", "3", "--workers", "2",
         *extra]
    )  # fmt: skip


def test_a_loud_song_fails_the_guard_and_its_record_says_so(tmp_path: Path) -> None:
    preset, run = make_run(tmp_path, {"loud": -3.0, "quiet": -30.0})

    assert guard(preset, run) == 1

    record = clipping_guard.read_record(preset)
    assert record is not None
    assert not record.passed
    assert record.blocks[0].song == "loud"
    assert record.blocks[0].worst_dbtp > -0.5
    assert record.fingerprint == clipping_guard.fingerprint(preset)
    assert record.lead_ins == 3
    assert record.run == "run"


def test_a_quiet_run_passes_and_judges_its_worst_blocks_worst_first(tmp_path: Path) -> None:
    preset, run = make_run(tmp_path, {"a": -24.0, "b": -30.0})

    assert guard(preset, run) == 0

    record = clipping_guard.read_record(preset)
    assert record is not None
    assert record.passed
    worst = [b.worst_dbtp for b in record.blocks]
    assert worst == sorted(worst, reverse=True)
    assert {b.song for b in record.blocks} <= {"a", "b"}


def test_a_run_at_another_bitrate_keeps_the_streams_record(tmp_path: Path) -> None:
    preset, run = make_run(tmp_path, {"a": -24.0})

    guard(preset, run)
    guard(preset, run, "--bitrate", "128k")

    at_stream = clipping_guard.read_record(preset)
    at_128k = clipping_guard.read_record(preset, "128k")
    assert at_stream is not None and at_stream.bitrate == codec.MP3_BITRATE
    assert at_128k is not None and at_128k.bitrate == "128k"
    # LAME turns 128k encodes down by 0.45 dB (codec.py), so their peaks decode lower.
    assert at_128k.blocks[0].worst_dbtp < at_stream.blocks[0].worst_dbtp


def test_a_run_without_the_presets_renders_is_an_error(tmp_path: Path) -> None:
    preset, run = make_run(tmp_path, {"a": -24.0})
    (run / "chain-x-v1").rename(run / "other-v1")

    with pytest.raises(SystemExit, match="chain-x-v1"):
        guard(preset, run)
