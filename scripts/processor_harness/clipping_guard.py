"""R17's clipping guard (Lance, 2026-10-26): on the worst transients in the corpus, at least 95%
of the MP3 encoder's states decode at or below -1.0 dBTP, and no state decodes above -0.3 dBTP.

The second figure was -0.5 until 2026-10-27 (Lance). The guard exists to keep every encoder state
under the decoder's 0 dBFS clip point, and -0.5 was a round margin, not a measured one. The
first guard run failed one block by 0.03 dB (In the Air Tonight 223.1 s, -0.468 dBTP, with 99.65%
of its states at or below -1.0). A lower ceiling is not guaranteed to clear it, since the tail is
not monotonic in the ceiling (Hotel California 155.2 s: -0.921 at -2.0 dBFS, -0.681 at -2.5).
-0.3 keeps a real margin under clipping and leaves room above the measured worst case.

The encoder's state when a song reaches it is random on air and moves a transient's decoded peak
by up to about 2 dB (see codec_states), so the guard judges a distribution, not one encode. Each
chain preset carries its last guard run at each bitrate beside it (``chain-x.guard.json``), with
a fingerprint of the preset and every Liquidsoap script its chain includes; a test fails when the
files no longer match a record (tests/scripts/processor_harness/test_clipping_guard_records.py).
The stream's bitrate must pass; the others are kept for a bitrate setting (Lance, 2026-10-28):
LAME turns 128k down by 0.45 dB and 256k not at all (codec.py), so the margin differs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from processor_harness.codec import MP3_BITRATE
from processor_harness.presets import load_preset

_INCLUDE = re.compile(r'^\s*%include\s+"([^"]+)"', re.MULTILINE)


@dataclass(frozen=True)
class Guard:
    limit_dbtp: float  # the decoded ceiling most states must hold
    share: float  # the share of states that must hold it
    clip_dbtp: float  # no state may decode above this


R17 = Guard(limit_dbtp=-1.0, share=0.95, clip_dbtp=-0.3)


@dataclass(frozen=True)
class Verdict:
    worst_dbtp: float
    share_at_or_below: float  # of the states, at or below the guard's limit
    passed: bool


def judge(guard: Guard, states_dbtp: Sequence[float]) -> Verdict:
    """One transient's decoded true peaks, one per encoder state, against the guard."""
    worst = max(states_dbtp)
    share = sum(v <= guard.limit_dbtp for v in states_dbtp) / len(states_dbtp)
    return Verdict(worst, share, _passes(guard, worst, share))


def _passes(guard: Guard, worst_dbtp: float, share_at_or_below: float) -> bool:
    return share_at_or_below >= guard.share and worst_dbtp <= guard.clip_dbtp


def _text(path: Path) -> bytes:
    # Line endings normalised: Git checks out CRLF on Windows and LF on the CI runner.
    return path.read_bytes().replace(b"\r\n", b"\n")


def chain_scripts(liq: Path) -> list[Path]:
    """The script and every script it includes, depth first, each once."""
    found: list[Path] = []

    def visit(path: Path) -> None:
        if path in found:
            return
        found.append(path)
        for name in _INCLUDE.findall(path.read_text(encoding="utf-8")):
            visit((path.parent / name).resolve())

    visit(liq.resolve())
    return found


def fingerprint(preset_path: Path) -> str:
    """SHA-256 over the preset file and every script its chain includes."""
    chain = load_preset(preset_path).chain
    digest = hashlib.sha256(_text(preset_path))
    for script in chain_scripts(chain) if chain else []:
        digest.update(_text(script))
    return digest.hexdigest()


@dataclass(frozen=True)
class JudgedBlock:
    song: str
    time_s: float
    worst_dbtp: float
    share_at_or_below: float
    p95_dbtp: float
    states_over_limit: int
    passed: bool


@dataclass(frozen=True)
class GuardRecord:
    preset: str
    fingerprint: str
    guard: Guard
    lead_ins: int  # encoder states per block
    run: str  # the run folder the renders came from
    passed: bool
    blocks: tuple[JudgedBlock, ...]  # the worst blocks, worst first
    bitrate: str = MP3_BITRATE  # the MP3 bitrate the states were encoded at


def rejudge(record: GuardRecord, guard: Guard) -> GuardRecord:
    """The record judged again under another guard with the same limit, from its measurements.
    Exact only while the record reaches every block with a state over the limit: past its last
    block, a block is only known to be no worse than that one."""
    if guard.limit_dbtp != record.guard.limit_dbtp:
        raise ValueError("the shares were counted against another limit; run the guard again")
    last = record.blocks[-1].worst_dbtp
    if last > guard.limit_dbtp:
        raise ValueError(
            f"the record ends at {last} dBTP, over the {guard.limit_dbtp} dBTP limit, so blocks it "
            "leaves out may fail; run the guard again"
        )
    blocks = tuple(
        replace(b, passed=_passes(guard, b.worst_dbtp, b.share_at_or_below)) for b in record.blocks
    )
    return replace(record, guard=guard, blocks=blocks, passed=all(b.passed for b in blocks))


def record_path(preset_path: Path) -> Path:
    return preset_path.with_suffix(".guard.json")


def _kept(path: Path) -> dict[str, GuardRecord]:
    """The guard file's records by bitrate, lowest first. Before R20 (2026-10-28) the file held
    one record, with no bitrate: it was judged at the stream's 128k then."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if "preset" in data:  # one record, as written before R20
        single = {"bitrate": "128k", **data}
        data = {single["bitrate"]: single}
    records = {
        bitrate: GuardRecord(
            preset=r["preset"],
            fingerprint=r["fingerprint"],
            guard=Guard(**r["guard"]),
            lead_ins=r["lead_ins"],
            run=r["run"],
            passed=r["passed"],
            blocks=tuple(JudgedBlock(**b) for b in r["blocks"]),
            bitrate=r["bitrate"],
        )
        for bitrate, r in data.items()
    }
    return {b: records[b] for b in _lowest_first(records)}


def _lowest_first(bitrates: Iterable[str]) -> list[str]:
    return sorted(bitrates, key=lambda b: int(b.removesuffix("k")))


def write_record(preset_path: Path, record: GuardRecord) -> None:
    """Keep ``record`` in the preset's guard file, beside its records at other bitrates."""
    path = record_path(preset_path)
    kept = _kept(path) | {record.bitrate: record}
    ordered = {b: asdict(kept[b]) for b in _lowest_first(kept)}
    path.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")


def read_record(preset_path: Path, bitrate: str = MP3_BITRATE) -> GuardRecord | None:
    """The preset's guard record at ``bitrate`` (by default the stream's), or None."""
    return _kept(record_path(preset_path)).get(bitrate)


def read_records(preset_path: Path) -> list[GuardRecord]:
    """Every guard record the preset keeps, lowest bitrate first."""
    return list(_kept(record_path(preset_path)).values())
