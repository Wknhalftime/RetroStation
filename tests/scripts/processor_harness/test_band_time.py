"""The band-1 release pick (Lance, 2026-10-25): how fast band 1 lets go after a loud bass
passage, and the THD its compressor adds on real bass, beside the 50 Hz tone."""

from __future__ import annotations

import math

import numpy as np
import pytest
from processor_harness import measure, signals
from processor_harness.corpus import GENERATORS
from processor_harness.measure import Rendered
from processor_harness.metrics import acompressor, bands, distortion
from processor_harness.metrics.acompressor import Settings

FS = 44100
EDGES = (100.0, 520.0, 1800.0, 6000.0)


def rendered(stim: signals.Stimulus, out: np.ndarray) -> Rendered:
    return Rendered(
        inp=stim.samples,
        out=out,
        fs=FS,
        segments=stim.segments,
        gauges={},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )


def test_a_level_drop_is_quiet_then_loud_then_quiet() -> None:
    stim = signals.level_drop(FS, 40.0, -17.0, -47.0, lead_s=2.0, loud_s=10.0, quiet_s=10.0)

    assert [(s.label, s.start_s, s.stop_s, s.level_dbfs) for s in stim.segments] == [
        ("idle", 0.0, 2.0, -47.0),
        ("loud", 2.0, 12.0, -17.0),
        ("after", 12.0, 22.0, -47.0),
    ]
    assert GENERATORS["level_drop"] is signals.level_drop


def test_recovery_is_the_time_back_within_1_db_of_idle_after_the_loud_part() -> None:
    # The chain adds 3 dB everywhere (makeup) and holds the quiet part 5 dB down after the
    # drop, letting go linearly over 2 s: within 1 dB of idle after 1.6 s.
    stim = signals.level_drop(FS, 40.0, -17.0, -47.0, lead_s=2.0, loud_s=10.0, quiet_s=10.0)
    t = np.arange(len(stim.samples)) / FS
    held = np.where(t >= 12.0, -5.0 + np.minimum(t - 12.0, 2.0) * 2.5, 0.0)
    held = np.where((t >= 2.0) & (t < 12.0), -3.0, held)
    out = stim.samples * 10 ** ((3.0 + held) / 20)[:, np.newaxis]

    row = measure.measure("level_drop", rendered(stim, out))

    assert row["recovery_to_1db_s"] == pytest.approx(1.6, abs=0.05)


def test_a_gain_that_never_lets_go_has_no_recovery() -> None:
    stim = signals.level_drop(FS, 40.0, -17.0, -47.0, lead_s=2.0, loud_s=4.0, quiet_s=4.0)
    t = np.arange(len(stim.samples)) / FS
    out = stim.samples * np.where(t >= 2.0, 0.5, 1.0)[:, np.newaxis]

    row = measure.measure("level_drop", rendered(stim, out))

    assert math.isnan(row["recovery_to_1db_s"])


def test_bass_thd_on_a_held_note_is_what_the_compressor_adds() -> None:
    # A held 45 Hz note is a steady, pitched window: the THD added there matches the model's
    # output measured directly.
    s = Settings.from_db(-26.0, 2.5, 9.03, 0.0, 30.0, 1000.0)
    x = signals.tone(FS, 45.0, -17.0, 6.0).samples
    band = np.stack([np.convolve(x[:, c], bands.kernels(EDGES, FS)[0], "same") for c in (0, 1)], 1)
    direct = 100 * (
        distortion.thd(acompressor.process(band, FS, s)[FS : 5 * FS], FS, 45.0)
        - distortion.thd(band[FS : 5 * FS], FS, 45.0)
    )

    result = bands.bass_thd_added(x, FS, EDGES, s)

    assert result.windows >= 5
    assert result.p50_pct == pytest.approx(direct, rel=0.1)
    assert result.p95_pct >= result.p50_pct


def test_noise_has_no_held_bass_notes() -> None:
    s = Settings.from_db(-26.0, 2.5, 9.03, 0.0, 30.0, 1000.0)
    x = signals.noise_with_pauses(FS, 6.0, 0.0, 1, -17.0).samples

    result = bands.bass_thd_added(x, FS, EDGES, s)

    assert result.windows == 0
    assert math.isnan(result.p50_pct)


def test_programme_through_a_band_preset_reports_band_1_bass_thd() -> None:
    s = Settings.from_db(-26.0, 2.5, 9.03, 0.0, 30.0, 1000.0)
    stim = signals.tone(FS, 45.0, -17.0, 6.0)
    r = Rendered(
        **{**rendered(stim, stim.samples).__dict__, "band_edges": EDGES, "band_models": (s,) * 5}
    )

    row = measure.measure(measure.PROGRAMME, r)

    assert row["band.1.bass_thd_added_p50_pct"] > 0.05
    assert row["band.1.bass_thd_windows"] >= 5


def test_real_songs_give_held_bass_notes_in_half_second_windows() -> None:
    # Notes change every quarter to half second in real songs: 1 s windows held within 1 dB
    # found 0-7 per song (Lance's corpus, 2026-10-25), half-second ones within 1.5 dB dozens.
    s = Settings.from_db(-26.0, 2.5, 9.03, 0.0, 30.0, 1000.0)
    t = np.arange(round(6.0 * FS)) / FS
    notes = np.where((t // 0.8) % 2 == 0, 45.0, 60.0)  # two notes, 0.8 s each
    decay_db = -17.0 - 3.0 * (t % 0.8) / 0.8  # each note re-attacks, then fades 3 dB
    mono = 10 ** (decay_db / 20) * np.sin(2 * np.pi * np.cumsum(notes) / FS)
    x = np.stack([mono, mono], axis=1)

    assert bands.bass_thd_added(x, FS, EDGES, s).windows >= 5
