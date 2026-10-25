"""Command line: per-band thresholds at which a multiband preset's compressors average a target
gain reduction over programme files, measured as the harness measures a render (metrics.bands).
The band step sets its starting thresholds this way (Lance, 2026-10-25: each band averaging
2-4 dB of gain reduction on the T3 renders). Each preset's ``threshold_db`` list is printed for
its file; a render through the preset then confirms it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from processor_harness.audio_io import read_wav
from processor_harness.metrics import bands
from processor_harness.presets import PresetError, load_preset


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find each band's threshold for a target mean gain reduction."
    )
    parser.add_argument("presets", nargs="+", type=Path, help="multiband preset TOML files")
    parser.add_argument("--inputs", nargs="+", type=Path, required=True, help="programme WAVs")
    parser.add_argument("--target-gr", type=float, default=3.0, help="mean gain reduction, dB")
    args = parser.parse_args(argv)
    try:
        presets = [load_preset(p) for p in args.presets]
    except PresetError as e:
        print(e, file=sys.stderr)
        return 2
    for preset in presets:
        if not preset.band_models:
            print(f"{preset.source}: no per-band compressor params to calibrate", file=sys.stderr)
            return 2
    for preset in presets:
        traces = []
        for path in args.inputs:
            x, fs = read_wav(path)
            traces.append(bands.calibration_traces(x, fs, preset.band_edges, preset.band_models))
        found = bands.thresholds_for_gain_reduction(traces, preset.band_models, args.target_gr)
        print(f'{preset.id}: threshold_db = "{" ".join(f"{t:.1f}" for t in found)}"')
    return 0
