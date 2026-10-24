"""End to end: one command renders the corpus through each preset, measures, and reports; the
shipped corpus and presets load."""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path

import pytest
from processor_harness import audio_io, cli, signals
from processor_harness.corpus import load_corpus
from processor_harness.presets import load_preset

FFMPEG = shutil.which("ffmpeg")
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
HARNESS = Path(__file__).resolve().parents[3] / "scripts" / "processor_harness"


def test_the_shipped_corpus_and_presets_load() -> None:
    corpus = load_corpus(HARNESS / "corpus.toml")
    presets = [load_preset(p) for p in sorted((HARNESS / "presets").glob("*.toml"))]

    assert corpus.sample_rate == 44100
    assert {s.kind for s in corpus.signals} >= {
        "impulse",
        "log_sweep",
        "stepped_tone",
        "tone_bursts",
        "noise_with_pauses",
    }
    assert {p.id for p in presets} >= {"bypass", "live-2026-10-22"}


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.skipif(FFMPEG is None or LIQUIDSOAP is None, reason="needs ffmpeg and LIQUIDSOAP_PATH")
def test_a_run_renders_measures_and_reports_every_preset(tmp_path: Path) -> None:
    assert FFMPEG is not None and LIQUIDSOAP is not None
    noise = signals.noise_with_pauses(44100, burst_s=8.0, pause_s=0.1, bursts=1, level_dbfs=-24.0)
    audio_io.write_wav(tmp_path / "song.wav", noise.samples, 44100)
    corpus = tmp_path / "corpus.toml"
    corpus.write_text(
        "sample_rate = 44100\n"
        '[[signal]]\nid = "impulse"\nkind = "impulse"\nlevel_dbfs = -30.0\nseconds = 1.0\n'
        "at_s = 0.25\n"
        '[[signal]]\nid = "tone"\nkind = "tone"\nfreq = 1000.0\nlevel_dbfs = -6.0\n'
        "seconds = 2.0\n"
        '[[programme]]\nid = "song"\npath = "song.wav"\ncategory = "music"\nstart_s = 0.0\n'
        "duration_s = 8.0\n",
        encoding="utf-8",
    )
    (tmp_path / "bypass.liq").write_text("def chain(s) = s end\n", encoding="utf-8")
    liq = tmp_path / "liq.toml"
    liq.write_text(
        'id = "liq-bypass"\nversion = 1\nbackend = "liquidsoap"\nchain = "bypass.liq"\n',
        encoding="utf-8",
    )
    ff = tmp_path / "ff.toml"
    ff.write_text(
        'id = "ff-bypass"\nversion = 1\nbackend = "ffmpeg"\ngraph = "[0:a]anull[out]"\n',
        encoding="utf-8",
    )
    out = tmp_path / "run"

    code = cli.main(
        ["--corpus", str(corpus), "--out", str(out), "--liquidsoap", LIQUIDSOAP,
         "--ffmpeg", FFMPEG, str(ff), str(liq)]
    )  # fmt: skip

    assert code == 0
    with (out / "summary.csv").open(encoding="utf-8") as f:
        rows = {r["preset"]: r for r in csv.DictReader(f)}
    assert set(rows) == {"ff-bypass", "liq-bypass"}
    assert rows["liq-bypass"]["renderer"].startswith("Liquidsoap 2.4.5")
    assert rows["ff-bypass"]["renderer"].startswith("ffmpeg version")
    for row in rows.values():
        assert row["standalone_ffmpeg"].startswith("ffmpeg version")
        assert abs(float(row["band_sum_min_db"])) < 0.05
        assert abs(float(row["band_sum_max_db"])) < 0.05
        assert float(row["lufs_i"]) == pytest.approx(-18.0, abs=0.1)
        assert float(row["thd_pct"]) < 0.1
        assert float(row["rtf"]) >= 0.0
        assert "codec_true_peak_dbtp" in row
    assert (out / "liq-bypass-v1.json").is_file()
    assert (out / "comparison.csv").is_file()
