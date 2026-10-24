"""Blind listening tests between two presets, so Lance's ear checks are recorded beside the
measurements.

- Clips are cut from each preset's 128k MP3 render (the stream's encoder), decoded, and set to
  the same integrated loudness, so the louder version cannot win by being louder. Matching only
  ever turns clips down, and further if needed to keep every peak under -0.5 dBFS.
- A session is 16 trials (ABX or preference). Each trial takes the next clip from a shuffled
  cycle and shuffles which preset is A and which is B (and, for ABX, which one is X). The key
  stays in the session file; the listener only ever sees A, B and X.
- Every pick is appended to the session's picks file as it is made, so a stopped session
  resumes where it left off.
- Scores use math item 13: p = sum over k >= c of C(n, k) 0.5^n. ABX is one-sided (correct
  identifications); preference is two-sided (neither preset is favoured in advance).
"""

from __future__ import annotations

import json
import math
import os
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import numpy as np

from processor_harness.audio_io import read_wav, write_pcm16
from processor_harness.codec import decode
from processor_harness.metrics import loudness

MAX_PEAK_DBFS = -0.5
_FADE_S = 0.01


class Mode(StrEnum):
    ABX = "abx"
    PREFERENCE = "preference"


@dataclass(frozen=True)
class Clip:
    id: str
    paths: Mapping[str, Path]  # preset id -> level-matched 16-bit WAV


@dataclass(frozen=True)
class Trial:
    number: int
    clip: str
    labels: Mapping[str, str]  # "A", "B" (and "X" for ABX) -> preset id


@dataclass(frozen=True)
class Session:
    mode: Mode
    presets: tuple[str, str]
    clips: tuple[Clip, ...]
    trials: tuple[Trial, ...]
    seed: int
    directory: Path | None = None

    def clip_path(self, clip: str, preset: str) -> Path:
        return next(c for c in self.clips if c.id == clip).paths[preset]

    @property
    def picks_path(self) -> Path:
        if self.directory is None:
            raise ValueError("session not saved yet: no picks file")
        return self.directory / f"{self.mode.value}-picks.jsonl"


@dataclass(frozen=True)
class Result:
    mode: Mode
    trials: int
    correct: int  # ABX: right identifications; preference: the leading preset's wins
    wins: Mapping[str, int]  # preference: picks per preset (empty for ABX)
    p_value: float
    per_clip: Mapping[str, str]  # clip -> "correct/total" (ABX) or "wins a/b" (preference)


class Player(Protocol):
    def play(self, path: Path) -> None: ...

    def stop(self) -> None: ...


class WinsoundPlayer:
    """Windows' own WAV player: play starts the clip from the top, stop silences it."""

    def play(self, path: Path) -> None:
        import winsound  # Windows only; imported here so the module loads anywhere

        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)

    def stop(self) -> None:
        import winsound

        # None stops whatever is playing (SND_PURGE is unsupported on current Windows).
        winsound.PlaySound(None, 0)


def binomial_p(correct: int, trials: int) -> float:
    """Math item 13: the chance of at least ``correct`` right out of ``trials`` by guessing."""
    favourable = sum(math.comb(trials, k) for k in range(correct, trials + 1))
    return float(favourable / 2**trials)


def _faded(x: np.ndarray, fs: int) -> np.ndarray:
    n = round(_FADE_S * fs)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    y = x.copy()
    y[:n] *= ramp[:, np.newaxis]
    y[-n:] *= ramp[::-1, np.newaxis]
    return y


def prepare_clip(
    clip_id: str,
    mp3s: Mapping[str, Path],
    start_s: float,
    duration_s: float,
    directory: Path,
    ffmpeg: Path,
) -> Clip:
    """Cut one clip from each preset's MP3 and set all of them to the same loudness."""
    directory.mkdir(parents=True, exist_ok=True)
    cuts: dict[str, np.ndarray] = {}
    fs = 0
    for preset, mp3 in mp3s.items():
        whole = decode(ffmpeg, mp3, directory / f"{clip_id}_{preset}.decoded.wav")
        decoded, fs = read_wav(whole)
        whole.unlink()  # a whole song as float: only needed long enough to cut the clip
        start = round(start_s * fs)
        cuts[preset] = decoded[start : start + round(duration_s * fs)]
    levels = {p: loudness.integrated(x, fs) for p, x in cuts.items()}
    target = min(levels.values())
    peaks = {p: loudness.to_db(float(np.max(np.abs(x)))) - levels[p] for p, x in cuts.items()}
    target = min(target, MAX_PEAK_DBFS - max(peaks.values()))
    paths = {}
    for preset, x in cuts.items():
        path = directory / f"{clip_id}_{preset}.wav"
        write_pcm16(path, _faded(x * 10 ** ((target - levels[preset]) / 20), fs), fs)
        paths[preset] = path
    return Clip(clip_id, paths)


def make_session(
    clips: Sequence[Clip],
    presets: tuple[str, str],
    mode: Mode,
    trials: int = 16,
    seed: int | None = None,
) -> Session:
    """Trials cycling through the clips in shuffled order, labels shuffled per trial."""
    seed = random.SystemRandom().randrange(2**32) if seed is None else seed
    rng = random.Random(seed)
    order: list[Clip] = []
    while len(order) < trials:
        batch = list(clips)
        rng.shuffle(batch)
        order += batch
    made = []
    for number, clip in enumerate(order[:trials], start=1):
        a, b = presets if rng.random() < 0.5 else (presets[1], presets[0])
        labels = {"A": a, "B": b}
        if mode is Mode.ABX:
            labels["X"] = rng.choice((a, b))
        made.append(Trial(number, clip.id, labels))
    return Session(mode, presets, tuple(clips), tuple(made), seed)


def _portable(path: Path, directory: Path) -> str:
    """A clip path relative to the session folder, so the folder can move as a whole; absolute
    when the clip is on another drive (no relative path exists then)."""
    target, base = path.resolve(), directory.resolve()
    if target.drive != base.drive:
        return str(target)
    return os.path.relpath(target, base)


def save_session(session: Session, directory: Path) -> Session:
    """Write the session (with its key) as ``<mode>-session.json`` under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": session.mode.value,
        "presets": list(session.presets),
        "seed": session.seed,
        "clips": {
            c.id: {p: _portable(path, directory) for p, path in c.paths.items()}
            for c in session.clips
        },
        "trials": [
            {"number": t.number, "clip": t.clip, "labels": dict(t.labels)} for t in session.trials
        ],
    }
    path = directory / f"{session.mode.value}-session.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return replace(session, directory=directory)


def load_session(path: Path) -> Session:
    """A session written by save_session."""
    data = json.loads(path.read_text(encoding="utf-8"))
    clips = tuple(
        Clip(clip_id, {p: (path.parent / v).resolve() for p, v in paths.items()})
        for clip_id, paths in data["clips"].items()
    )
    trials = tuple(Trial(t["number"], t["clip"], t["labels"]) for t in data["trials"])
    presets = (data["presets"][0], data["presets"][1])
    return Session(Mode(data["mode"]), presets, clips, trials, data["seed"], path.parent)


def read_picks(session: Session) -> list[dict[str, object]]:
    """The picks recorded so far, in the order they were made."""
    path = session.picks_path
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _prompt(session: Session, trial: Trial) -> str:
    total = len(session.trials)
    if session.mode is Mode.ABX:
        keys = "a / b / x play, s stop; 1 = X is A, 2 = X is B; q quit"
    else:
        keys = "a / b play, s stop; 1 = prefer A, 2 = prefer B; q quit"
    return f"[{session.mode.value} {trial.number}/{total}] {keys} > "


def run_session(session: Session, player: Player, ask: Callable[[str], str] = input) -> None:
    """Ask for a pick on every unanswered trial, recording each one as it is made."""
    done = {p["trial"] for p in read_picks(session)}
    playable = ("a", "b", "x") if session.mode is Mode.ABX else ("a", "b")
    for trial in session.trials:
        if trial.number in done:
            continue
        while True:
            command = ask(_prompt(session, trial)).strip().lower()
            if command in playable:
                player.stop()
                player.play(session.clip_path(trial.clip, trial.labels[command.upper()]))
            elif command == "s":
                player.stop()
            elif command in ("1", "2"):
                player.stop()
                pick = {
                    "trial": trial.number,
                    "clip": trial.clip,
                    "choice": "A" if command == "1" else "B",
                    "time": datetime.now(UTC).isoformat(timespec="seconds"),
                }
                with session.picks_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(pick) + "\n")
                break
            elif command == "q":
                player.stop()
                return


def score(session: Session, picks: Sequence[Mapping[str, object]]) -> Result:
    """ABX: correct identifications, one-sided p. Preference: wins per preset, two-sided p."""
    trials = {t.number: t for t in session.trials}
    per_clip: dict[str, list[str]] = {}
    if session.mode is Mode.ABX:
        correct = 0
        for pick in picks:
            t = trials[int(str(pick["trial"]))]
            right = t.labels[str(pick["choice"])] == t.labels["X"]
            correct += right
            per_clip.setdefault(t.clip, []).append("1" if right else "0")
        summary = {c: f"{marks.count('1')}/{len(marks)}" for c, marks in per_clip.items()}
        return Result(
            session.mode, len(picks), correct, {}, binomial_p(correct, len(picks)), summary
        )
    wins = dict.fromkeys(session.presets, 0)
    for pick in picks:
        t = trials[int(str(pick["trial"]))]
        preset = t.labels[str(pick["choice"])]
        wins[preset] += 1
        per_clip.setdefault(t.clip, []).append(preset)
    leader = max(wins.values())
    summary = {
        c: " / ".join(f"{p} {chosen.count(p)}" for p in session.presets)
        for c, chosen in per_clip.items()
    }
    p_value = min(1.0, 2 * binomial_p(leader, len(picks)))
    return Result(session.mode, len(picks), leader, wins, p_value, summary)
