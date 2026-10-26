"""Linear behaviour of a chain: band-sum flatness and latency from the impulse (math item 6),
frequency response from the log sweep (math item 7). The left channel is analysed; the chains
are stereo-linked and the stimuli have identical channels.

Responses are output spectrum over input spectrum, so the chain's overall gain (an AGC's idle
gain, say) cancels when results are read relative to 1 kHz.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Audio = NDArray[np.float64]

REFERENCE_HZ = 1000.0
BAND_LO_HZ = 20.0
BAND_HI_HZ = 20000.0
# Third-octave centres 1000 * 2^(k/3), 25 Hz .. 16 kHz (the 20 kHz band crosses Nyquist).
THIRD_OCTAVE_CENTRES = 1000.0 * 2.0 ** (np.arange(-16, 13) / 3.0)
# Their ISO 266 nominal labels, used in metric names.
THIRD_OCTAVE_LABELS = (
    "25", "31.5", "40", "50", "63", "80", "100", "125", "160", "200", "250", "315", "400", "500",
    "630", "800", "1000", "1250", "1600", "2000", "2500", "3150", "4000", "5000", "6300", "8000",
    "10000", "12500", "16000",
)  # fmt: skip


def _spectra(
    inp: Audio, out: Audio, fs: int
) -> tuple[NDArray[np.float64], NDArray[np.complex128], NDArray[np.complex128]]:
    n = max(len(inp), len(out))
    freqs = np.asarray(np.fft.rfftfreq(n, 1 / fs), dtype=np.float64)
    return freqs, np.fft.rfft(inp[:, 0], n), np.fft.rfft(out[:, 0], n)


def band_sum_range(inp: Audio, out: Audio, fs: int) -> tuple[float, float]:
    """Lowest and highest response (dB, relative to 1 kHz) from 20 Hz to 20 kHz, per FFT bin.

    For an impulse through the crossover with every band at unity, this is the band-sum
    flatness the brief accepts at +-0.1 dB.
    """
    freqs, x, y = _spectra(inp, out, fs)
    with np.errstate(divide="ignore"):
        mag = 20 * np.log10(np.abs(y) / np.abs(x))
    reference = mag[np.argmin(np.abs(freqs - REFERENCE_HZ))]
    band = mag[(freqs >= BAND_LO_HZ) & (freqs <= BAND_HI_HZ)] - reference
    return float(np.min(band)), float(np.max(band))


def latency(inp: Audio, out: Audio, fs: int) -> float:
    """Delay of the output behind the input, s: the cross-correlation peak at lags >= 0."""
    n = len(inp) + len(out)
    corr = np.fft.irfft(np.fft.rfft(out[:, 0], n) * np.conj(np.fft.rfft(inp[:, 0], n)), n)
    return float(np.argmax(np.abs(corr[: len(out)]))) / fs


def third_octave_response(
    inp: Audio, out: Audio, fs: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Output-over-input power per third-octave band (centres 25 Hz .. 16 kHz), dB."""
    freqs, x, y = _spectra(inp, out, fs)
    px, py = np.abs(x) ** 2, np.abs(y) ** 2
    levels = []
    for centre in THIRD_OCTAVE_CENTRES:
        band = (freqs >= centre * 2 ** (-1 / 6)) & (freqs < centre * 2 ** (1 / 6))
        levels.append(10 * np.log10(np.sum(py[band]) / np.sum(px[band])))
    return THIRD_OCTAVE_CENTRES, np.asarray(levels, dtype=np.float64)


def sweep_deviation(inp: Audio, out: Audio, fs: int) -> float:
    """Largest third-octave departure from the 1 kHz band, dB (dynamics idle at -30 dBFS)."""
    centres, levels = third_octave_response(inp, out, fs)
    reference = levels[np.argmin(np.abs(centres - REFERENCE_HZ))]
    return float(np.max(np.abs(levels - reference)))


def _transfer(
    inp: Audio, out: Audio, fs: int
) -> tuple[NDArray[np.float64], NDArray[np.complex128]]:
    freqs, x, y = _spectra(inp, out, fs)
    with np.errstate(divide="ignore", invalid="ignore"):
        return freqs, np.asarray(y / x, dtype=np.complex128)


def response_at_db(inp: Audio, out: Audio, fs: int, freq: float) -> float:
    """The chain's gain (dB) at one frequency, from the impulse (high-pass corner checks)."""
    freqs, h = _transfer(inp, out, fs)
    return float(20 * np.log10(np.interp(freq, freqs, np.abs(h))))


def group_delay_ms(inp: Audio, out: Audio, fs: int, freq: float) -> float:
    """The group delay at one frequency beyond the group delay at 1 kHz (ms): what an IIR
    stage adds at the low end, the chain's plain latency taken out."""
    freqs, h = _transfer(inp, out, fs)
    phase = np.unwrap(np.angle(h))
    delay = -np.gradient(phase, 2 * np.pi * freqs)
    return float(1000 * (np.interp(freq, freqs, delay) - np.interp(REFERENCE_HZ, freqs, delay)))
