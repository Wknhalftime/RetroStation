"""R18 remedy 3 (Lance, 2026-10-27): before the R17 chain goes into session.liq, the script-cache
warm-up must cover every script of it. The app warms the cache at startup (D34) and again when
``script_fingerprint`` changes (#184), so every R17 script must move the fingerprint. A cold start
of the R17 chain took 10.7 s against the 5 s ready timeout (D37).

The R17 chain's scripts come from the harness's own include walk (the one its clipping-guard
fingerprint uses), not from the backend's, so the two walks must agree."""

from __future__ import annotations

import shutil
from pathlib import Path

from processor_harness import clipping_guard
from processor_harness.presets import load_preset

from backend.playout.script_cache import script_fingerprint

REPO = Path(__file__).resolve().parents[3]
R17 = REPO / "scripts" / "processor_harness" / "presets" / "limiter" / "chain-r17.toml"


def test_the_warm_up_covers_every_script_of_the_r17_chain(tmp_path: Path) -> None:
    chain = load_preset(R17).chain
    assert chain is not None
    walk = clipping_guard.chain_scripts(chain)
    # The harness's wrappers under scripts/ only point at the chain; what airs is under backend/.
    scripts = [s.relative_to(REPO) for s in walk if s.is_relative_to(REPO / "backend")]
    assert Path("backend/playout/chain.liq") in scripts
    for script in scripts:  # each in its place in the repository, so includes resolve as there
        (tmp_path / script).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / script, tmp_path / script)
    session = tmp_path / "backend" / "playout" / "session_r17.liq"  # beside session.liq
    session.write_text('%include "chain.liq"\n', encoding="utf-8")  # as session.liq does
    warmed = script_fingerprint(session)

    uncovered = []
    for script in scripts:
        copy = tmp_path / script
        original = copy.read_bytes()
        copy.write_bytes(original + b"\n# edited\n")
        if script_fingerprint(session) == warmed:
            uncovered.append(script.as_posix())
        copy.write_bytes(original)

    assert not uncovered, f"the cache warm-up would miss an edit to {uncovered}"
    assert len(scripts) >= 6  # chain.liq, agc.liq and the four chain_*.liq stages
