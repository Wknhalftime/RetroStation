"""Playlists (sequence inputs): songs joined into one continuous render, so the AGC's state
carries across song boundaries as it does on the stream (Lance, 2026-10-24: the time to reach
within 1 dB of target after each boundary and each +-6 dB level error)."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, measure, pipeline, signals
from processor_harness.corpus import CorpusError, load_corpus
from processor_harness.measure import Rendered
from processor_harness.metrics import loudness
from processor_harness.prepare import PreparedInput, prepare_inputs
from processor_harness.presets import load_preset
from processor_harness.signals import Segment

FS = 44100
FFMPEG = shutil.which("ffmpeg")


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def song_corpus(tmp_path: Path, extra: str) -> Path:
    for name in ("a", "b"):
        if not (tmp_path / f"{name}.wav").exists():
            (tmp_path / f"{name}.wav").write_bytes(b"")
    return write(
        tmp_path / "c.toml",
        "sample_rate = 44100\n"
        '[[programme]]\nid = "a"\npath = "a.wav"\ncategory = "music"\nstart_s = 0.0\n'
        "duration_s = 4.0\n"
        '[[programme]]\nid = "b-6db"\npath = "b.wav"\ncategory = "music"\nstart_s = 0.0\n'
        "duration_s = 4.0\noffset_db = -6.0\n" + extra,
    )


def test_a_sequence_lists_programme_entries_in_play_order(tmp_path: Path) -> None:
    corpus = load_corpus(
        song_corpus(tmp_path, '[[sequence]]\nid = "list"\nitems = ["b-6db", "a"]\n')
    )

    (seq,) = corpus.sequences
    assert (seq.id, seq.items) == ("list", ("b-6db", "a"))


def test_a_sequence_can_only_name_programme_entries(tmp_path: Path) -> None:
    path = song_corpus(tmp_path, '[[sequence]]\nid = "list"\nitems = ["a", "nope"]\n')

    with pytest.raises(CorpusError, match=r"c\.toml.*items"):
        load_corpus(path)


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_a_sequence_joins_the_prepared_songs_with_labelled_boundaries(tmp_path: Path) -> None:
    noise = signals.noise_with_pauses(FS, burst_s=4.0, pause_s=0.1, bursts=1, level_dbfs=-20.0)
    for name in ("a", "b"):
        audio_io.write_wav(tmp_path / f"{name}.wav", noise.samples, FS)
    corpus = load_corpus(
        song_corpus(tmp_path, '[[sequence]]\nid = "list"\nitems = ["b-6db", "a"]\n')
    )

    assert FFMPEG is not None
    prepared = {p.id: p for p in prepare_inputs(corpus, tmp_path / "inputs", ffmpeg=Path(FFMPEG))}

    seq = prepared["list"]
    assert seq.kind == measure.SEQUENCE
    assert [(s.label, s.start_s, s.level_dbfs) for s in seq.segments] == [
        ("b-6db", 0.0, -24.0),
        ("a", 4.0, -18.0),
    ]
    x, _ = audio_io.read_wav(seq.path)
    assert len(x) == 8 * FS
    assert loudness.integrated(x[: 4 * FS], FS) == pytest.approx(-24.0, abs=0.05)


def test_sequence_renders_get_their_boundaries_as_a_param(tmp_path: Path) -> None:
    write(tmp_path / "chain.liq", "def chain(s) = s end\n")
    preset = load_preset(
        write(
            tmp_path / "p.toml",
            'id = "x"\nversion = 1\nbackend = "liquidsoap"\nchain = "chain.liq"\n'
            "[params]\ntarget = -18.0\n",
        )
    )
    segments = (Segment(0.0, 4.0, -24.0, "b"), Segment(4.0, 8.0, -18.0, "a"))
    seq = PreparedInput("list", measure.SEQUENCE, "", tmp_path / "s.wav", segments)
    song = PreparedInput("a", measure.PROGRAMME, "music", tmp_path / "a.wav", segments[:1])

    assert pipeline.render_params(preset, seq) == {"target": -18.0, "boundaries": "0.0 4.0"}
    assert pipeline.render_params(preset, song) == {"target": -18.0}


def test_presets_name_the_gauge_and_target_a_level_check_uses(tmp_path: Path) -> None:
    preset = load_preset(
        write(
            tmp_path / "p.toml",
            'id = "x"\nversion = 1\nbackend = "ffmpeg"\ngraph = "g"\n'
            '[level_check]\ngauge = "agc_gain"\ntarget_lufs = -18.0\n',
        )
    )

    assert preset.level_check == ("agc_gain", -18.0)


def sequence_render(gain: np.ndarray, times: np.ndarray) -> Rendered:
    # Two songs of 30 s: the first arrives 6 dB hot (needs -6 dB), the second at level (0 dB).
    noise = signals.noise_with_pauses(FS, burst_s=60.0, pause_s=0.1, bursts=1, level_dbfs=-18.0)
    segments = (Segment(0.0, 30.0, -12.0, "hot"), Segment(30.0, 60.0, -18.0, "level"))
    return Rendered(
        inp=noise.samples,
        out=noise.samples,
        fs=FS,
        segments=segments,
        gauges={"agc_gain": (times, gain)},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
        gauge_limits={"agc_gain": (-10.0, 10.0)},
        level_check=("agc_gain", -18.0),
    )


def test_recovery_is_the_time_to_come_within_1_db_of_the_needed_correction() -> None:
    times = np.arange(0.0, 60.0, 0.02)
    # Down to -6 dB at 1 dB/s from the start; back up to 0 at 0.5 dB/s after the boundary.
    gain = np.where(
        times < 30.0, -np.minimum(times, 6.0), -6.0 + np.minimum(times - 30.0, 12.0) * 0.5
    )

    row = measure.measure(measure.SEQUENCE, sequence_render(gain, times))

    assert row["recovery_s.01-hot"] == pytest.approx(5.0, abs=0.05)  # within 1 dB of -6
    assert row["recovery_s.02-level"] == pytest.approx(10.0, abs=0.05)  # within 1 dB of 0
    assert row["recovery_s_max"] == pytest.approx(10.0, abs=0.05)
    assert row["recovery_missed"] == 0.0


def test_a_correction_never_reached_counts_as_missed() -> None:
    times = np.arange(0.0, 60.0, 0.02)
    gain = np.full(len(times), -6.0)  # stuck: never recovers for the second song

    row = measure.measure(measure.SEQUENCE, sequence_render(gain, times))

    assert np.isnan(row["recovery_s.02-level"])
    assert row["recovery_missed"] == 1.0


def test_sequences_report_each_songs_output_loudness_and_their_spread() -> None:
    times = np.arange(0.0, 60.0, 0.02)
    render = sequence_render(np.zeros(len(times)), times)
    out = render.out.copy()
    out[30 * FS :] *= 10 ** (-3 / 20)  # the second song comes out 3 dB quieter
    render = Rendered(**{**render.__dict__, "out": out})

    row = measure.measure(measure.SEQUENCE, render)

    assert row["item_lufs_spread"] == pytest.approx(3.0, abs=0.1)
    assert row["item_lufs.01-hot"] - row["item_lufs.02-level"] == pytest.approx(3.0, abs=0.1)
