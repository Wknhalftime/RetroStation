"""Command line for blind listening tests. Run through scripts/run_listening_test.py.

    prepare --run DIR --clips FILE --a PRESET --b PRESET --name NAME
        cut level-matched clips from the run's MP3 renders of both presets and write an ABX
        and a preference session (16 trials each) to DIR/listening/NAME
    run DIR/listening/NAME --mode abx|preference
        play the trials blind (Windows' own WAV player) and record every pick
    score DIR/listening/NAME
        score whatever has been picked: <mode>-results.json and results.md

The clips file names the run's programme inputs and where each clip starts:

    duration_s = 15.0          # default clip length, 10 s to 2 minutes
    [[clip]]
    input = "teen-spirit"      # a programme id from the run's corpus
    start_s = 0.0              # within the rendered excerpt
    duration_s = 69.0          # optional: this clip's own length
    note = "quiet intro into the band"
    id = "teen-intro"          # optional; defaults to <input>-<start>s, unique per test
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from processor_harness import listening
from processor_harness.listening import Mode, Result
from processor_harness.presets import PresetError, load_preset

MIN_CLIP_S = 10.0
MAX_CLIP_S = 120.0  # long enough for whole passages (Lance, 2026-10-24)


class ClipSpecError(ValueError):
    """The clips file is missing a field or has a bad value."""


@dataclass(frozen=True)
class ClipSpec:
    id: str
    input: str
    start_s: float
    duration_s: float
    note: str


def _length(path: Path, value: object) -> float:
    if not isinstance(value, int | float) or not MIN_CLIP_S <= value <= MAX_CLIP_S:
        raise ClipSpecError(f"{path}: duration_s must be {MIN_CLIP_S:g} to {MAX_CLIP_S:g} s")
    return float(value)


def load_clip_specs(path: Path) -> list[ClipSpec]:
    """The clips to cut, each with its length (its own duration_s, else the file's);
    errors name the file."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    default = data.get("duration_s")
    entries = data.get("clip", [])
    if not entries:
        raise ClipSpecError(f"{path}: no [[clip]] entries")
    specs = []
    for entry in entries:
        if "input" not in entry or "start_s" not in entry:
            raise ClipSpecError(f"{path}: every [[clip]] needs input and start_s")
        start = float(entry["start_s"])
        length = _length(path, entry.get("duration_s", default))
        clip_id = str(entry.get("id", f"{entry['input']}-{start:g}s"))
        note = str(entry.get("note", ""))
        specs.append(ClipSpec(clip_id, str(entry["input"]), start, length, note))
    ids = [spec.id for spec in specs]
    if len(ids) != len(set(ids)):
        raise ClipSpecError(f"{path}: clip ids must be unique, got {ids}")
    return specs


def _prepare(args: argparse.Namespace) -> None:
    specs = load_clip_specs(args.clips)
    presets = [load_preset(args.a), load_preset(args.b)]
    folder = args.run / "listening" / args.name
    clips = []
    for spec in specs:
        mp3s = {}
        for preset in presets:
            mp3 = args.run / f"{preset.id}-v{preset.version}" / f"{spec.input}.mp3"
            if not mp3.is_file():
                raise FileNotFoundError(f"no MP3 render of {spec.input!r} for {preset.id}: {mp3}")
            mp3s[preset.id] = mp3
        clips.append(
            listening.prepare_clip(
                spec.id, mp3s, spec.start_s, spec.duration_s, folder / "clips", args.ffmpeg
            )
        )
    pair = (presets[0].id, presets[1].id)
    for mode in Mode:
        session = listening.make_session(clips, pair, mode, trials=args.trials)
        listening.save_session(session, folder)
    notes = {
        s.id: {"input": s.input, "start_s": s.start_s, "duration_s": s.duration_s, "note": s.note}
        for s in specs
    }
    (folder / "clips.json").write_text(json.dumps(notes, indent=2) + "\n", encoding="utf-8")
    print(f"prepared {len(clips)} clips; run: run_listening_test.py run {folder} --mode abx")


def _run(args: argparse.Namespace) -> None:
    session = listening.load_session(args.folder / f"{args.mode}-session.json")
    listening.run_session(session, listening.WinsoundPlayer())


def _report_line(result: Result) -> str:
    if result.mode is Mode.ABX:
        return (
            f"| ABX | {result.correct}/{result.trials} correct | "
            f"p = {result.p_value:.4f} (one-sided) |"
        )
    wins = ", ".join(f"{p} {n}" for p, n in result.wins.items())
    return (
        f"| Preference | {wins}, no preference {result.no_preference} | "
        f"p = {result.p_value:.4f} (two-sided, over the decided trials) |"
    )


def _score(args: argparse.Namespace) -> None:
    lines = ["| Mode | Score | p-value |", "|---|---|---|"]
    details = []
    for mode in Mode:
        path = args.folder / f"{mode.value}-session.json"
        if not path.is_file():
            continue
        session = listening.load_session(path)
        picks = listening.read_picks(session)
        if not picks:
            continue
        result = listening.score(session, picks)
        (args.folder / f"{mode.value}-results.json").write_text(
            json.dumps(
                {
                    "mode": mode.value,
                    "presets": list(session.presets),
                    "trials": result.trials,
                    "correct": result.correct,
                    "wins": dict(result.wins),
                    "no_preference": result.no_preference,
                    "p_value": result.p_value,
                    "per_clip": dict(result.per_clip),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        lines.append(_report_line(result))
        details += [f"- {mode.value} {clip}: {s}" for clip, s in result.per_clip.items()]
    report = "\n".join(lines + [""] + details) + "\n"
    (args.folder / "results.md").write_text(report, encoding="utf-8")
    print(report)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Blind listening tests between two presets.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="cut clips and write the sessions")
    prepare.add_argument("--run", type=Path, required=True, help="harness run directory")
    prepare.add_argument("--clips", type=Path, required=True)
    prepare.add_argument("--a", type=Path, required=True, help="first preset TOML")
    prepare.add_argument("--b", type=Path, required=True, help="second preset TOML")
    prepare.add_argument("--name", required=True, help="folder under <run>/listening")
    prepare.add_argument("--trials", type=int, default=16)
    prepare.add_argument("--ffmpeg", type=Path, default=shutil.which("ffmpeg"))
    run = commands.add_parser("run", help="play one session blind and record the picks")
    run.add_argument("folder", type=Path)
    run.add_argument("--mode", choices=[m.value for m in Mode], required=True)
    score = commands.add_parser("score", help="score the picks made so far")
    score.add_argument("folder", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    commands = {"prepare": _prepare, "run": _run, "score": _score}
    try:
        commands[args.command](args)
    except (ClipSpecError, PresetError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
