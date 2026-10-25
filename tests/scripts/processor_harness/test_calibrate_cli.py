"""The threshold-calibration command: per-band thresholds at which a multiband preset's
compressors average a target gain reduction over programme files (Lance, 2026-10-25: thresholds
set so each band averages 2-4 dB of gain reduction on the T3 renders)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from processor_harness import audio_io, calibrate_cli, signals
from processor_harness.metrics import bands
from processor_harness.presets import load_preset

FS = 44100
PRESET = (
    'id = "gentle"\nversion = 1\nbackend = "liquidsoap"\nchain = "chain.liq"\n'
    '[params]\nsplit = "520 1800"\nthreshold_db = "0 0 0"\nratio = "2.5 2.5 2.5"\n'
    'knee_db = "9.03 9.03 9.03"\nmakeup_db = "0 0 0"\nattack_ms = "34 33 25"\n'
    'release_ms = "1000 1000 1000"\ndetection = 1\nlink = 0\n'
)


def test_prints_each_presets_thresholds_for_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "chain.liq").write_text("def chain(s) = s end\n", encoding="utf-8")
    preset_path = tmp_path / "gentle.toml"
    preset_path.write_text(PRESET, encoding="utf-8")
    songs = []
    for seed, level in ((1, -16.0), (2, -22.0)):
        x = signals.noise_with_pauses(FS, 6.0, 0.5, 1, level, seed=seed).samples
        songs.append(tmp_path / f"song{seed}.wav")
        audio_io.write_wav(songs[-1], x, FS)

    code = calibrate_cli.main(
        [str(preset_path), "--inputs", *map(str, songs), "--target-gr", "3.0"]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert 'gentle: threshold_db = "' in printed
    values = [float(v) for v in printed.split('threshold_db = "')[1].split('"')[0].split()]
    preset = load_preset(preset_path)
    traces = [
        bands.calibration_traces(audio_io.read_wav(p)[0], FS, preset.band_edges, preset.band_models)
        for p in songs
    ]
    expected = bands.thresholds_for_gain_reduction(traces, preset.band_models, 3.0)
    assert values == pytest.approx(expected, abs=0.051)  # printed to 0.1 dB
    assert 'gentle: makeup_db = "' in printed
    makeups = [float(v) for v in printed.split('makeup_db = "')[1].split('"')[0].split()]
    tuned = [
        replace(m, threshold=10 ** (t / 20))
        for m, t in zip(preset.band_models, expected, strict=True)
    ]
    assert makeups == pytest.approx(bands.makeups_for_balance(traces, tuned), abs=0.051)


def test_a_preset_without_band_compressors_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    preset_path = tmp_path / "plain.toml"
    preset_path.write_text(
        'id = "plain"\nversion = 1\nbackend = "ffmpeg"\ngraph = "[0:a]anull[out]"\n',
        encoding="utf-8",
    )
    song = tmp_path / "song.wav"
    audio_io.write_wav(song, signals.tone(FS, 1000.0, -20.0, 1.0).samples, FS)

    code = calibrate_cli.main([str(preset_path), "--inputs", str(song)])

    assert code == 2
    assert "plain.toml" in capsys.readouterr().err
