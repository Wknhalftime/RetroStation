"""Per-band fits (the band step, Lance 2026-10-25): a stimulus aimed at one band of a multiband
preset is fitted against that band's compressor settings, so the stepped tones confirm every
band does what it was set to."""

from __future__ import annotations

from pathlib import Path

import pytest
from processor_harness import pipeline
from processor_harness.corpus import CorpusError, load_corpus
from processor_harness.prepare import PreparedInput, prepare_inputs
from processor_harness.presets import load_preset

TONE = (
    'sample_rate = 44100\n[[signal]]\nid = "steps-band2"\nkind = "stepped_tone"\nfreq = 228.0\n'
    "start_dbfs = -40.0\nstop_dbfs = 0.0\nhold_s = 0.2\nband = 2\n"
)
BANDS = (
    'id = "x"\nversion = 1\nbackend = "liquidsoap"\nchain = "chain.liq"\n'
    '[params]\nsplit = "100 520"\nthreshold_db = "-30 -28 -26"\nratio = "2 3 4"\n'
    'knee_db = "9.03 9.03 9.03"\nmakeup_db = "0 0 0"\nattack_ms = "34 33 25"\n'
    'release_ms = "1000 1000 1000"\ndetection = 1\nlink = 0\n'
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_a_signal_can_name_the_band_it_is_aimed_at(tmp_path: Path) -> None:
    (entry,) = load_corpus(write(tmp_path / "c.toml", TONE)).signals

    assert entry.band == 2
    assert "band" not in entry.params


def test_the_band_must_be_a_whole_number_from_1(tmp_path: Path) -> None:
    path = write(tmp_path / "c.toml", TONE.replace("band = 2", "band = 0"))

    with pytest.raises(CorpusError, match=r"c\.toml.*band"):
        load_corpus(path)


def test_prepared_signals_keep_their_band(tmp_path: Path) -> None:
    corpus = load_corpus(write(tmp_path / "c.toml", TONE))

    (prepared,) = prepare_inputs(corpus, tmp_path / "inputs", ffmpeg=None)

    assert prepared.band == 2


def test_a_band_stimulus_is_fitted_against_that_bands_settings(tmp_path: Path) -> None:
    write(tmp_path / "chain.liq", "def chain(s) = s end\n")
    preset = load_preset(write(tmp_path / "p.toml", BANDS))
    aimed = PreparedInput("steps-band2", "stepped_tone", "", tmp_path / "x.wav", (), band=2)
    plain = PreparedInput("steps", "stepped_tone", "", tmp_path / "x.wav", ())

    assert pipeline.model_for(preset, aimed) == preset.band_models[1]
    assert pipeline.model_for(preset, plain) is preset.model


def test_a_band_past_the_presets_bands_is_refused(tmp_path: Path) -> None:
    write(tmp_path / "chain.liq", "def chain(s) = s end\n")
    preset = load_preset(write(tmp_path / "p.toml", BANDS))
    aimed = PreparedInput("steps-band9", "stepped_tone", "", tmp_path / "x.wav", (), band=9)

    with pytest.raises(ValueError, match="steps-band9"):
        pipeline.model_for(preset, aimed)
