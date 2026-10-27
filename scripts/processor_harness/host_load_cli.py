"""Command line: the host's capacity for a chain preset (R18, Lance 2026-10-27: re-measure on any
host change). Start-up per tune-in with the script cache warm and cold, then each session count
at once: CPU per session, p99.9 frame load (pass mark: under 50%), late ticks and catch-ups.
Sessions run pinned like production's (``STREAM_RESERVED_CORES``); the command keeps to the
other cores. Writes ``host_load.json`` and prints a table."""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from backend.config import Settings
from backend.playout.cpu_reservation import physical_cores, reserved_cpus
from processor_harness import affinity, host_load
from processor_harness.corpus import load_corpus
from processor_harness.presets import load_preset

DEFAULT_SESSIONS = (1, 2, 4, 6, 8, 10, 12)


def main(argv: Sequence[str] | None = None) -> int:
    affinity.stay_off_stream_cores()  # R18: off the listener sessions' cores
    parser = argparse.ArgumentParser(description="The host's capacity for a chain preset.")
    parser.add_argument("preset", type=Path, help="a Liquidsoap preset's TOML file")
    parser.add_argument("--corpus", type=Path, required=True, help="its music is the playlist")
    parser.add_argument("--out", type=Path, required=True, help="run folder (var/harness/…)")
    parser.add_argument("--liquidsoap", type=Path, default=os.environ.get("LIQUIDSOAP_PATH"))
    parser.add_argument("--sessions", type=int, nargs="+", default=list(DEFAULT_SESSIONS))
    parser.add_argument("--warm", type=float, default=20.0, help="seconds before measuring")
    parser.add_argument("--seconds", type=float, default=180.0, help="seconds measured")
    parser.add_argument("--starts", type=int, default=10, help="tune-ins timed, warm and cold")
    args = parser.parse_args(argv)
    if args.liquidsoap is None:
        parser.error("--liquidsoap or LIQUIDSOAP_PATH is required")
    preset = load_preset(args.preset)
    songs = [e.path for e in load_corpus(args.corpus).programme if e.category == "music"]
    if not songs:
        parser.error(f"{args.corpus} has no music to play")
    cpus = reserved_cpus(physical_cores(), Settings().stream_reserved_cores)
    script = host_load.write_session_script(preset, args.out / "session_load.liq")

    warm = host_load.warm_starts(args.liquidsoap, script, str(songs[0]), args.out, args.starts)
    cold = host_load.cold_starts(args.liquidsoap, script, str(songs[0]), args.out, args.starts)
    for name, start in (("warm", warm), ("cold", cold)):
        cpu, wall = statistics.median(start.cpu_s), statistics.median(start.wall_s)
        print(f"start-up, {name} cache: CPU {cpu:.2f} s, first frame after {wall:.2f} s (median)")
    rows = []
    for n in args.sessions:
        result = host_load.run_sessions(args.liquidsoap, script, songs, args.out / f"n{n}", n,
                                        args.warm, args.seconds, cpus)  # fmt: skip
        worst = max(load.p999_share for load in result.loads)
        print(f"{n:3d} sessions on CPUs {list(cpus) or 'any'}: "
              f"CPU {statistics.median(result.cpu_per_session):.1%} of a thread each, "
              f"{result.total_threads:.2f} threads, machine {result.machine_cpu_pct:.0f}%; "
              f"p99.9 frame load worst {worst:.0%} ({'pass' if worst < 0.5 else 'FAIL'}); "
              f"late ticks {sum(load.late for load in result.loads)}; "
              f"catch-ups {result.catchups}", flush=True)  # fmt: skip
        rows.append({**asdict(result), "worst_p999_share": worst})
    report = {
        "preset": preset.id,
        "cpus": list(cpus),
        "startup": {"warm": asdict(warm), "cold": asdict(cold)},
        "concurrency": rows,
    }
    (args.out / "host_load.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0
