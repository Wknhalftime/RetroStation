"""session.liq processes the stream with the R17 chain (Lance, 2026-10-27: on air after the
acceptance test): the chain the harness measured and the clipping guard passed, at the guarded
preset's values (chain_params.liq), its AGC held as each song starts. That its processing
sounds exactly like the harness's render of chain-r17 is checked in
tests/scripts/processor_harness/test_session_liq_chain.py."""

from __future__ import annotations

from backend.playout.liquidsoap_process import SESSION_SCRIPT


def test_session_liq_runs_the_guarded_chain_and_holds_its_agc_at_each_song() -> None:
    text = SESSION_SCRIPT.read_text(encoding="utf-8")

    assert '%include "chain_params.liq"' in text
    assert '%include "chain.liq"' in text  # beside it: the chain needs nothing from scripts/
    assert "processed = chain(s)" in text
    assert "song_started := processed.hold" in text
    assert '%include "multiband.liq"' not in text  # the old Optimod-modelled chain is gone
