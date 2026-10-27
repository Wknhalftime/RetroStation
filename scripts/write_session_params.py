"""Write backend/playout/chain_params.liq, the parameters session.liq runs the R17 chain with,
from the preset the clipping guard passed. See processor_harness/session_params.py.

    uv run python scripts/write_session_params.py
"""

from pathlib import Path

from processor_harness import session_params
from processor_harness.presets import load_preset

REPO = Path(__file__).resolve().parents[1]
PRESET = REPO / "scripts" / "processor_harness" / "presets" / "limiter" / "chain-r17.toml"
OUT = REPO / "backend" / "playout" / "chain_params.liq"

if __name__ == "__main__":
    OUT.write_text(session_params.script(load_preset(PRESET)), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(REPO)} from {PRESET.relative_to(REPO)}")
