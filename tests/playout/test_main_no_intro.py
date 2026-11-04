"""The prepared engine plays no tune-in intro (spec D114: the static intro is withdrawn)."""

from __future__ import annotations

import os
from pathlib import Path

from backend.main import _prepared_engine
from backend.playout.liquidsoap_process import session_base_env


async def test_prepared_engine_has_no_intro(tmp_path: Path, liquidsoap_exe: Path) -> None:
    for folder in ("assets", "cache", "logs"):
        (tmp_path / folder).mkdir()
    prepared = await _prepared_engine(
        "ffmpeg", liquidsoap_exe, tmp_path, session_base_env(os.environ)
    )
    assert prepared is not None
    engine, _ = prepared
    assert engine.intro_sfx is None
    assert engine.filler.is_file()
