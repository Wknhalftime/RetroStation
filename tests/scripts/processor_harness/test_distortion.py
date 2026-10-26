"""THD and twin-tone IMD (brief math item 12), against signals with known products."""

from __future__ import annotations

import numpy as np
import pytest
from processor_harness import signals
from processor_harness.metrics import distortion

FS = 44100


def stereo(mono: np.ndarray) -> np.ndarray:
    return np.column_stack([mono, mono])


def test_a_clean_tone_has_almost_no_thd() -> None:
    s = signals.tone(FS, 1000.0, -6.0, 3.0).samples

    assert distortion.thd(s, FS, 1000.0) < 0.001


def test_a_one_percent_second_harmonic_reads_one_percent_thd() -> None:
    t = np.arange(3 * FS) / FS
    mono = 0.5 * np.sin(2 * np.pi * 1000.0 * t) + 0.005 * np.sin(2 * np.pi * 2000.0 * t)

    assert distortion.thd(stereo(mono), FS, 1000.0) * 100 == pytest.approx(1.0, rel=0.02)


def test_clean_smpte_twin_tone_has_almost_no_imd() -> None:
    s = signals.smpte_twin_tone(FS, level_dbfs=-6.0, seconds=3.0).samples

    assert distortion.imd_smpte(s, FS) < 0.001


def test_smpte_sidebands_around_7khz_read_as_imd() -> None:
    t = np.arange(3 * FS) / FS
    hi = 0.1
    mono = 0.4 * np.sin(2 * np.pi * 60.0 * t) + hi * np.sin(2 * np.pi * 7000.0 * t)
    mono += 0.01 * hi * (np.sin(2 * np.pi * 6940.0 * t) + np.sin(2 * np.pi * 7060.0 * t))

    # Two sidebands each 1% of the 7 kHz tone: sqrt(2) %.
    assert distortion.imd_smpte(stereo(mono), FS) * 100 == pytest.approx(np.sqrt(2), rel=0.03)


def test_ccif_difference_tone_reads_as_imd() -> None:
    t = np.arange(3 * FS) / FS
    mono = 0.25 * (np.sin(2 * np.pi * 19000.0 * t) + np.sin(2 * np.pi * 20000.0 * t))
    clean = distortion.imd_ccif(stereo(mono), FS)
    mono += 0.005 * np.sin(2 * np.pi * 1000.0 * t)

    # 0.005 against two 0.25 tones: 0.005 / sqrt(2 x 0.25^2) = 1.41 %.
    assert clean < 0.001
    assert distortion.imd_ccif(stereo(mono), FS) * 100 == pytest.approx(1.414, rel=0.03)
