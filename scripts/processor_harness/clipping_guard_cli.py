"""Command line: R17's clipping guard on one run of a chain preset, its verdict written beside the
preset as the guard record that tests/scripts/processor_harness/test_clipping_guard_records.py
checks.

Screening encodes every render (whole, never an excerpt) at ten lead-ins and keeps each block's
worst decoded peak. The songs holding the 20 worst blocks are then encoded at every lead-in of one
MP3 frame, up to 2 s past their last screened block, and every block up to there is judged:
screening by song, so a block whose bad state is rare is still judged over all of them (at
-2.5 dBFS, ten lead-ins missed Hotel California 152.8 s, where 1 state in 1152 reaches -0.973).
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from processor_harness import clipping_guard
from processor_harness.audio_io import Audio, read_wav
from processor_harness.clipping_guard import R17, GuardRecord, JudgedBlock
from processor_harness.codec_states import LEAD_INS, block_peaks, worst_blocks
from processor_harness.metrics.loudness import STEP_S
from processor_harness.presets import load_preset

SCREEN_LEAD_INS = (0, 37, 144, 288, 432, 577, 720, 864, 1008, 1100)
SCREEN_BLOCKS = 20
SEPARATION_S = 2.0  # blocks closer than this in one song are one transient
TAIL_S = 2.0  # how far past its last screened block a swept song is encoded
RECORDED_BLOCKS = 20

Job = tuple[str, str, int, float | None]  # ffmpeg, render, lead-in, cut (s) or None
_loaded: dict[tuple[str, float | None], tuple[Audio, int]] = {}


def _peaks(job: Job) -> tuple[str, int, NDArray[np.float64]]:
    """One render's decoded block peaks at one lead-in (runs in a worker process). Jobs come
    song by song, so each worker keeps only the render it is on."""
    ffmpeg, render, lead_in, cut_s = job
    if (render, cut_s) not in _loaded:
        _loaded.clear()
        x, fs = read_wav(Path(render))
        _loaded[(render, cut_s)] = (x if cut_s is None else x[: round(cut_s * fs)], fs)
    x, fs = _loaded[(render, cut_s)]
    return render, lead_in, block_peaks(Path(ffmpeg), x, fs, lead_in)


def _renders(run: Path, preset_id: str, version: int) -> list[Path]:
    folder = run / f"{preset_id}-v{version}"
    if not folder.is_dir():
        raise SystemExit(f"{run} has no renders of the preset (expected {folder.name}/)")
    return sorted(
        p for p in folder.glob("*.wav")
        if not p.name.endswith(".decoded.wav") and not p.name.startswith("_")
    )  # fmt: skip


def _judged(song: str, states: NDArray[np.float64]) -> list[JudgedBlock]:
    """Every block of one song, from its peaks at each lead-in (rows) per block (columns)."""
    blocks = []
    for i in range(states.shape[1]):
        column = states[:, i]
        verdict = clipping_guard.judge(R17, column.tolist())
        blocks.append(
            JudgedBlock(
                song=song,
                time_s=round(i * STEP_S, 1),
                worst_dbtp=round(verdict.worst_dbtp, 3),
                share_at_or_below=round(verdict.share_at_or_below, 4),
                p95_dbtp=round(float(np.percentile(column, 95)), 3),
                states_over_limit=int((column > R17.limit_dbtp).sum()),
                passed=verdict.passed,
            )
        )
    return blocks


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R17's clipping guard on one run of a preset.")
    parser.add_argument("preset", type=Path, help="the chain preset's TOML file")
    parser.add_argument("run", type=Path, help="the run folder its renders are in (var/harness/…)")
    parser.add_argument("--ffmpeg", type=Path, default=shutil.which("ffmpeg"))
    parser.add_argument("--lead-ins", type=int, default=LEAD_INS, help="states per block")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    preset = load_preset(args.preset)
    renders = _renders(args.run, preset.id, preset.version)
    ffmpeg = str(args.ffmpeg)

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        screen = [(ffmpeg, str(r), k, None) for r in renders for k in SCREEN_LEAD_INS]
        screened: dict[str, NDArray[np.float64]] = {}
        for render, _, peaks in pool.map(_peaks, screen, chunksize=len(SCREEN_LEAD_INS)):
            seen = screened.get(render)
            n = len(peaks) if seen is None else min(len(seen), len(peaks))
            screened[render] = peaks[:n] if seen is None else np.maximum(seen[:n], peaks[:n])
        worst = worst_blocks(screened, SCREEN_BLOCKS, SEPARATION_S)
        cuts = {b.song: max(c.time_s for c in worst if c.song == b.song) + TAIL_S for b in worst}
        names = ", ".join(Path(s).stem for s in cuts)
        print(f"screened {len(renders)} renders; sweeping {len(cuts)} songs at {args.lead_ins} "
              f"lead-ins: {names}", flush=True)  # fmt: skip

        sweep = [(ffmpeg, song, k, cut) for song, cut in cuts.items() for k in range(args.lead_ins)]
        rows: dict[str, list[NDArray[np.float64]]] = {song: [] for song in cuts}
        for render, _, peaks in pool.map(_peaks, sweep, chunksize=4):
            rows[render].append(peaks)

    judged = []
    for song, peaks_by_lead_in in rows.items():
        n = min(len(p) for p in peaks_by_lead_in)
        judged += _judged(Path(song).stem, np.stack([p[:n] for p in peaks_by_lead_in]))
    judged.sort(key=lambda b: -b.worst_dbtp)
    passed = all(b.passed for b in judged)
    record = GuardRecord(
        preset=preset.id,
        fingerprint=clipping_guard.fingerprint(args.preset),
        guard=R17,
        lead_ins=args.lead_ins,
        run=args.run.name,
        passed=passed,
        blocks=tuple(judged[:RECORDED_BLOCKS]),
    )
    clipping_guard.write_record(args.preset, record)
    for b in record.blocks[:10]:
        print(f"  {b.song:24} {b.time_s:7.1f} s  worst {b.worst_dbtp:+.3f}  p95 {b.p95_dbtp:+.3f}  "
              f"{b.share_at_or_below:.1%} at or below {R17.limit_dbtp}  "
              f"{'pass' if b.passed else 'FAIL'}")  # fmt: skip
    print(f"{'PASSED' if passed else 'FAILED'}: {len(judged)} blocks judged -> "
          f"{clipping_guard.record_path(args.preset).name}")  # fmt: skip
    return 0 if passed else 1
