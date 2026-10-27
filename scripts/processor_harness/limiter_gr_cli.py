"""Command line: each limiter variant's gain reduction against the no-limiter reference, per
input, written beside the variant's run folder as ``<folder>.limiter_gr.json``, with a line
per variant: the median and worst of the inputs' p95 gain reduction."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Sequence
from pathlib import Path

from processor_harness import affinity
from processor_harness.limiter_gr import compare_runs


def main(argv: Sequence[str] | None = None) -> int:
    affinity.stay_off_stream_cores()  # R18: off the listener sessions' cores
    parser = argparse.ArgumentParser(description="Limiter gain reduction against a reference.")
    parser.add_argument("--reference", type=Path, required=True, help="no-limiter run folder")
    parser.add_argument("variants", nargs="+", type=Path, help="limiter run folders")
    args = parser.parse_args(argv)
    for variant in args.variants:
        rows = compare_runs(args.reference, variant)
        path = variant.with_name(variant.name + ".limiter_gr.json")
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        p95 = [r["lim_gr_p95_db"] for r in rows.values() if not math.isnan(r["lim_gr_p95_db"])]
        if p95:
            print(
                f"{variant.name}: p95 gain reduction median {statistics.median(p95):.2f} dB, "
                f"worst {max(p95):.2f} dB ({len(p95)} inputs) -> {path.name}"
            )
    return 0
