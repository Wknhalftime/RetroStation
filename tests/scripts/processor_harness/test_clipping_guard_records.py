"""Every chain preset carries a passing clipping-guard record made from the files as they are now
(R17; Lance, 2026-10-26). Changing a chain preset, or any Liquidsoap script its chain includes,
fails this test until the guard is run again:

    uv run python scripts/run_clipping_guard.py <preset.toml> var/harness/<run>

after rendering the preset over the corpus into var/harness/<run>."""

from __future__ import annotations

from pathlib import Path

import pytest
from processor_harness import clipping_guard

PRESETS = Path(__file__).resolve().parents[3] / "scripts" / "processor_harness" / "presets"
CHAIN_PRESETS = sorted((PRESETS / "limiter").glob("chain-*.toml"))


def test_there_is_a_chain_preset_to_guard() -> None:
    assert CHAIN_PRESETS


@pytest.mark.parametrize("preset", CHAIN_PRESETS, ids=lambda p: p.stem)
def test_the_chain_preset_has_a_fresh_passing_guard_record(preset: Path) -> None:
    record = clipping_guard.read_record(preset)

    assert record is not None, f"{preset.name} has no clipping-guard record; run the guard"
    assert record.fingerprint == clipping_guard.fingerprint(preset), (
        f"{preset.name} or a script its chain includes changed since its guard record was "
        f"made (run {record.run}); render it again and re-run the guard"
    )
    assert record.passed, f"{preset.name} failed the clipping guard: {record.blocks[:3]}"
