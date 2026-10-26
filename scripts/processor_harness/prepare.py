"""Input preparation: every corpus entry becomes a 32-bit float stereo WAV at the corpus rate.

Stimuli are generated; programme excerpts are cut and resampled by FFmpeg, then set to the
corpus's programme_lufs, the level songs reach the chain at in the stream (the decks apply each
item's gain first), so the AGC sees what it sees live. An entry can move off that level by
offset_db, or keep its mastered level (native), to give the AGC level variation to work on.
A recorded drum intro can have everything before its first hit silenced (isolate_first_hit):
its room sound sits only 30-40 dB under the hit, which would swamp pre-ringing measured down to
-60 dB.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from processor_harness.audio_io import Audio, read_wav, write_wav
from processor_harness.corpus import Corpus, ProgrammeEntry
from processor_harness.measure import PROGRAMME, SEQUENCE
from processor_harness.metrics import loudness
from processor_harness.signals import Segment

_HIT_DB = -12.0  # the first hit: where a 1 ms envelope first comes within 12 dB of the peak
_HIT_WINDOW_S = 0.001


@dataclass(frozen=True)
class PreparedInput:
    id: str
    kind: str  # a signal kind, or "programme"
    category: str  # programme category; "" for stimuli
    path: Path
    segments: tuple[Segment, ...]


def _excerpt(ffmpeg: Path, entry: ProgrammeEntry, fs: int, target_lufs: float, out: Path) -> float:
    """Cut, resample and level one excerpt; its integrated loudness as written (LUFS)."""
    run = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-nostats", "-y",
            "-ss", str(entry.start_s), "-t", str(entry.duration_s), "-i", str(entry.path),
            "-ac", "2", "-ar", str(fs), "-c:a", "pcm_f32le", str(out),
        ],
        capture_output=True,
        encoding="utf-8", errors="replace",
    )  # fmt: skip
    if run.returncode != 0:
        raise RuntimeError(f"ffmpeg could not cut {entry.path}:\n{run.stderr[-4000:]}")
    x, _ = read_wav(out)
    if entry.isolate_first_hit:
        x = _isolate_first_hit(x, fs)
    level = loudness.integrated(x, fs)
    target = level if entry.native else target_lufs + entry.offset_db
    write_wav(out, x * 10 ** ((target - level) / 20), fs)
    return target


def _isolate_first_hit(x: Audio, fs: int) -> Audio:
    """Zero everything before the first hit's attack. The 1 ms trailing envelope crosses
    -12 dB re peak up to 1 ms into the attack, so the cut goes 1 ms before that crossing."""
    window = round(_HIT_WINDOW_S * fs)
    power = np.mean(x * x, axis=1)
    running = np.concatenate([np.zeros(window), np.cumsum(power)])
    envelope = np.sqrt((running[window:] - running[:-window]) / window)
    crossing = int(np.argmax(envelope >= np.max(envelope) * 10 ** (_HIT_DB / 20)))
    isolated = x.copy()
    isolated[: max(0, crossing - window)] = 0.0
    return isolated


def prepare_inputs(
    corpus: Corpus, directory: Path, ffmpeg: Path | None
) -> tuple[PreparedInput, ...]:
    """Write every input under ``directory``; stimuli first, in manifest order."""
    if corpus.programme and ffmpeg is None:
        raise ValueError("the corpus has programme entries, which need ffmpeg to cut them")
    directory.mkdir(parents=True, exist_ok=True)
    fs = corpus.sample_rate
    prepared = []
    for entry in corpus.signals:
        stimulus = entry.build(fs)
        path = directory / f"{entry.id}.wav"
        write_wav(path, stimulus.samples, fs)
        prepared.append(PreparedInput(entry.id, entry.kind, "", path, stimulus.segments))
    for item in corpus.programme:
        assert ffmpeg is not None
        path = directory / f"{item.id}.wav"
        level = _excerpt(ffmpeg, item, fs, corpus.programme_lufs, path)
        span = (Segment(0.0, item.duration_s, level),)
        prepared.append(PreparedInput(item.id, PROGRAMME, item.category, path, span))
    songs = {p.id: p for p in prepared if p.kind == PROGRAMME}
    for sequence in corpus.sequences:
        prepared.append(_join(sequence.id, [songs[i] for i in sequence.items], directory, fs))
    return tuple(prepared)


def _join(sequence_id: str, songs: list[PreparedInput], directory: Path, fs: int) -> PreparedInput:
    """The songs end to end in one file, each segment labelled with its song and level."""
    parts, segments, t = [], [], 0.0
    for song in songs:
        x, _ = read_wav(song.path)
        parts.append(x)
        stop = t + len(x) / fs
        segments.append(Segment(t, stop, song.segments[0].level_dbfs, song.id))
        t = stop
    path = directory / f"{sequence_id}.wav"
    write_wav(path, np.concatenate(parts), fs)
    return PreparedInput(sequence_id, SEQUENCE, "", path, tuple(segments))


def silence(directory: Path, fs: int, seconds: float = 1.0) -> Path:
    """A short silent file: rendering it measures a chain's startup cost (for the RTF)."""
    path = directory / "_startup.wav"
    write_wav(path, np.zeros((round(seconds * fs), 2)), fs)
    return path
