"""Input preparation: stimuli to WAV with their schedules; programme excerpts cut, made stereo
44.1 kHz, and set to the loudness songs reach the chain at (the decks' item gain)."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, signals
from processor_harness.corpus import load_corpus
from processor_harness.metrics import loudness
from processor_harness.prepare import prepare_inputs

FS = 44100
FFMPEG = shutil.which("ffmpeg")


def corpus_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "corpus.toml"
    path.write_text("sample_rate = 44100\n" + body, encoding="utf-8")
    return path


def test_stimuli_are_written_with_their_kind_and_schedule(tmp_path: Path) -> None:
    corpus = load_corpus(
        corpus_file(
            tmp_path,
            '[[signal]]\nid = "steps"\nkind = "stepped_tone"\nfreq = 1000.0\n'
            "start_dbfs = -10.0\nstop_dbfs = 0.0\nhold_s = 0.2\n",
        )
    )

    (steps,) = prepare_inputs(corpus, tmp_path / "inputs", ffmpeg=None)

    assert (steps.id, steps.kind, steps.category) == ("steps", "stepped_tone", "")
    assert [s.level_dbfs for s in steps.segments] == [-10.0, -8.0, -6.0, -4.0, -2.0, 0.0]
    x, fs = audio_io.read_wav(steps.path)
    assert fs == FS and x.shape == (round(6 * 0.2 * FS), 2)


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_programme_is_cut_resampled_and_set_to_the_item_gain_level(tmp_path: Path) -> None:
    # A 48 kHz mono source at about -30 LUFS: the excerpt must come out stereo 44.1 kHz at -18.
    noise = signals.noise_with_pauses(48000, burst_s=20.0, pause_s=0.1, bursts=1, level_dbfs=-33.0)
    audio_io.write_wav(tmp_path / "song.wav", noise.samples[:, :1], 48000)
    corpus = load_corpus(
        corpus_file(
            tmp_path,
            "programme_lufs = -18.0\n"
            '[[programme]]\nid = "song"\npath = "song.wav"\ncategory = "music"\n'
            "start_s = 5.0\nduration_s = 10.0\n",
        )
    )

    assert FFMPEG is not None
    (song,) = prepare_inputs(corpus, tmp_path / "inputs", ffmpeg=Path(FFMPEG))

    assert (song.kind, song.category) == ("programme", "music")
    x, fs = audio_io.read_wav(song.path)
    assert fs == FS and x.shape[1] == 2
    assert len(x) / FS == pytest.approx(10.0, abs=0.01)
    assert loudness.integrated(x, FS) == pytest.approx(-18.0, abs=0.05)
    np.testing.assert_array_equal(x[:, 0], x[:, 1])


def test_programme_without_ffmpeg_is_refused(tmp_path: Path) -> None:
    (tmp_path / "song.wav").write_bytes(b"")
    corpus = load_corpus(
        corpus_file(
            tmp_path,
            '[[programme]]\nid = "song"\npath = "song.wav"\ncategory = "music"\n'
            "start_s = 0.0\nduration_s = 1.0\n",
        )
    )

    with pytest.raises(ValueError, match="ffmpeg"):
        prepare_inputs(corpus, tmp_path / "inputs", ffmpeg=None)


def test_corpus_programme_level_defaults_to_the_item_gain_target(tmp_path: Path) -> None:
    assert load_corpus(corpus_file(tmp_path, "")).programme_lufs == -18.0
