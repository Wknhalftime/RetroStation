"""The corpus manifest: which synthetic stimuli and programme excerpts every preset is rendered
through. Synthetic entries name a generator in signals.py (``kind``) and its parameters;
programme entries point at a file (relative paths resolve next to the manifest) and the excerpt
to cut from it.

    sample_rate = 44100
    programme_lufs = -18.0     # optional: the item-gain level songs reach the chain at
    [[signal]]
    id = "impulse"
    kind = "impulse"
    level_dbfs = -30.0
    seconds = 2.0
    at_s = 0.5
    [[programme]]
    id = "teen-spirit"
    path = "D:/Music/..."
    category = "music"        # music | speech | voice | transient
    start_s = 0.0
    duration_s = 60.0
    offset_db = -10.0         # optional: this many dB from programme_lufs (AGC level tests)
    native = true             # optional, instead: keep the level it was mastered at
    isolate_first_hit = true  # optional: silence everything before the first hit's attack
"""

from __future__ import annotations

import inspect
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from processor_harness import signals
from processor_harness.signals import Stimulus

GENERATORS: dict[str, Callable[..., Stimulus]] = {
    "impulse": signals.impulse,
    "log_sweep": signals.log_sweep,
    "stepped_tone": signals.stepped_tone,
    "tone_bursts": signals.tone_bursts,
    "tone": signals.tone,
    "smpte_imd": signals.smpte_twin_tone,
    "ccif_imd": signals.ccif_twin_tone,
    "noise_with_pauses": signals.noise_with_pauses,
    "kick": signals.kick,
    "snare": signals.snare,
}
CATEGORIES = ("music", "speech", "voice", "transient")
# Songs reach the chain at -18 LUFS: the decks apply each item's gain (liq_amplify) first.
DEFAULT_PROGRAMME_LUFS = -18.0


class CorpusError(ValueError):
    """The manifest is missing a field or has a bad value."""


@dataclass(frozen=True)
class SignalEntry:
    id: str
    kind: str
    params: Mapping[str, float | int]

    def build(self, fs: int) -> Stimulus:
        return GENERATORS[self.kind](fs, **self.params)


@dataclass(frozen=True)
class ProgrammeEntry:
    id: str
    path: Path
    category: str
    start_s: float
    duration_s: float
    offset_db: float = 0.0
    native: bool = False
    isolate_first_hit: bool = False


@dataclass(frozen=True)
class Corpus:
    sample_rate: int
    programme_lufs: float
    signals: tuple[SignalEntry, ...]
    programme: tuple[ProgrammeEntry, ...]
    source: Path


def _signal(path: Path, entry: dict[str, object]) -> SignalEntry:
    entry_id, kind = str(entry.get("id", "")), str(entry.get("kind", ""))
    if kind not in GENERATORS:
        raise CorpusError(f"{path}: signal {entry_id!r}: unknown kind {kind!r}")
    params = {k: v for k, v in entry.items() if k not in ("id", "kind")}
    try:
        inspect.signature(GENERATORS[kind]).bind(0, **params)
    except TypeError as error:
        raise CorpusError(f"{path}: signal {entry_id!r}: {error}") from None
    numbers = {k: v for k, v in params.items() if isinstance(v, int | float)}
    return SignalEntry(entry_id, kind, numbers)


def _programme(path: Path, entry: dict[str, object]) -> ProgrammeEntry:
    entry_id = str(entry.get("id", ""))
    file = (path.parent / str(entry.get("path", ""))).resolve()
    if not file.is_file():
        raise CorpusError(f"{path}: programme {entry_id!r}: path not found: {file}")
    category = str(entry.get("category", ""))
    if category not in CATEGORIES:
        raise CorpusError(
            f"{path}: programme {entry_id!r}: category {category!r} is not one of "
            + ", ".join(CATEGORIES)
        )
    native = entry.get("native", False) is True
    if native and "offset_db" in entry:
        raise CorpusError(
            f"{path}: programme {entry_id!r}: native and offset_db exclude each other"
        )
    return ProgrammeEntry(
        entry_id,
        file,
        category,
        float(str(entry.get("start_s", 0.0))),
        float(str(entry.get("duration_s", 0.0))),
        float(str(entry.get("offset_db", 0.0))),
        native,
        entry.get("isolate_first_hit", False) is True,
    )


def load_corpus(path: Path) -> Corpus:
    """Read and validate the manifest; errors name the file and the field."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("sample_rate"), int):
        raise CorpusError(f"{path}: missing whole-number field 'sample_rate'")
    found = tuple(_signal(path, e) for e in data.get("signal", []))
    programme = tuple(_programme(path, e) for e in data.get("programme", []))
    ids = [e.id for e in found] + [e.id for e in programme]
    if len(ids) != len(set(ids)):
        raise CorpusError(f"{path}: every id must be unique, got {ids}")
    level = float(data.get("programme_lufs", DEFAULT_PROGRAMME_LUFS))
    return Corpus(int(data["sample_rate"]), level, found, programme, path)
