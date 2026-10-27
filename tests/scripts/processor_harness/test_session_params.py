"""session.liq runs the R17 chain with the guarded preset's values (Lance, 2026-10-27: on air
after the acceptance test). Its params file is generated from chain-r17.toml, the preset the
clipping guard passed, and this test fails when the two differ."""

from __future__ import annotations

from pathlib import Path

from processor_harness import session_params
from processor_harness.presets import load_preset

REPO = Path(__file__).resolve().parents[3]
R17 = REPO / "scripts" / "processor_harness" / "presets" / "limiter" / "chain-r17.toml"
PARAMS = REPO / "backend" / "playout" / "chain_params.liq"


def test_session_liq_runs_the_guarded_presets_values() -> None:
    on_air = PARAMS.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")

    assert on_air == session_params.script(load_preset(R17)), (
        "backend/playout/chain_params.liq differs from chain-r17.toml; regenerate it: "
        "uv run python scripts/write_session_params.py"
    )


def test_the_params_script_defines_what_the_chain_reads() -> None:
    text = session_params.script(load_preset(R17))

    for name in ("param_float", "param_int", "param_bool", "param_string"):
        assert f"def {name}(name, default) =" in text
    assert '("limit_db", -2.5)' in text
    assert '("oversample", 4)' in text
    assert "chain-r17.toml" in text  # it names where its values come from
