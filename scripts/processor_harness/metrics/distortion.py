"""THD and twin-tone IMD (brief math item 12), as ratios (x 100 for percent).

The brief notes these predict audibility poorly (THD r = -0.42 against listener ratings); they
are bench checks on the dynamics stages, not quality scores. Each analysis uses the middle of
the render (the first and last 10% are skipped, so attack transients don't count), a
Blackman-Harris window, and a tone's power summed over its main lobe (+-4 bins).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy import signal

Audio = NDArray[np.float64]

_SKIP_FRACTION = 0.1
_LOBE_BINS = 4
_HARMONICS = 10


def _power_spectrum(x: Audio, fs: int) -> tuple[float, NDArray[np.float64]]:
    """Bin width (Hz) and power spectrum of the windowed middle section, left channel."""
    skip = round(_SKIP_FRACTION * len(x))
    mono = x[skip : len(x) - skip, 0]
    spectrum = np.fft.rfft(mono * signal.get_window("blackmanharris", len(mono)))
    return fs / len(mono), np.abs(spectrum) ** 2


def _tone_power(bin_hz: float, power: NDArray[np.float64], freq: float) -> float:
    k = round(freq / bin_hz)
    return float(np.sum(power[max(k - _LOBE_BINS, 0) : k + _LOBE_BINS + 1]))


def thd(x: Audio, fs: int, f0: float) -> float:
    """Harmonics 2-10 (those below Nyquist) over the fundamental, RMS ratio."""
    bin_hz, power = _power_spectrum(x, fs)
    nyquist = fs / 2 - _LOBE_BINS * bin_hz
    harmonics = [k * f0 for k in range(2, _HARMONICS + 1) if k * f0 < nyquist]
    total = sum(_tone_power(bin_hz, power, f) for f in harmonics)
    return float(np.sqrt(total / _tone_power(bin_hz, power, f0)))


def imd_smpte(x: Audio, fs: int, f1: float = 60.0, f2: float = 7000.0, orders: int = 4) -> float:
    """SMPTE IMD: sidebands f2 +- n f1 (n = 1..4) over the f2 tone, RMS ratio."""
    bin_hz, power = _power_spectrum(x, fs)
    products = [f2 + sign * n * f1 for n in range(1, orders + 1) for sign in (-1, 1)]
    total = sum(_tone_power(bin_hz, power, f) for f in products)
    return float(np.sqrt(total / _tone_power(bin_hz, power, f2)))


def imd_ccif(x: Audio, fs: int, f1: float = 19000.0, f2: float = 20000.0) -> float:
    """CCIF twin-tone IMD: the difference tone f2 - f1 and the third-order products 2f1 - f2
    and 2f2 - f1 (those below Nyquist) over both tones, RMS ratio."""
    bin_hz, power = _power_spectrum(x, fs)
    nyquist = fs / 2 - _LOBE_BINS * bin_hz
    products = [f for f in (f2 - f1, 2 * f1 - f2, 2 * f2 - f1) if f < nyquist]
    total = sum(_tone_power(bin_hz, power, f) for f in products)
    tones = _tone_power(bin_hz, power, f1) + _tone_power(bin_hz, power, f2)
    return float(np.sqrt(total / tones))
