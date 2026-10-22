"""A script edited while the app runs still starts sessions (D113).

The bug (2026-10-22): the cache was warmed only at startup. An edited ``session.liq`` (or a
file it includes) compiles in about 5.3 s, over D37's 5 s readiness deadline, so every
session was killed before it wrote the cache, and every ``/listen`` was 503 until a restart.
Real Liquidsoap, on copies of the scripts, with a cache of their own (never the shared one).
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")

from stream_stub import StubItem, StubSession, start_stub, stub_base_url  # noqa: E402

from backend.playout.assets import ensure_stream_assets  # noqa: E402
from backend.playout.liquidsoap_process import (  # noqa: E402
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    free_port,
    long_path,
    start_ready_engine,
    warm_script_cache,
)
from backend.playout.script_cache import ScriptCache, warm_first  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]


@pytest.fixture
def job() -> Iterator[object]:
    with windows_job.KillOnCloseJob() as owned:
        yield owned


def copy_scripts(into: Path) -> Path:
    """Every ``.liq`` beside ``session.liq``, copied; the copy of ``session.liq``."""
    into.mkdir()
    for script in SESSION_SCRIPT.parent.glob("*.liq"):
        shutil.copy2(script, into / script.name)
    return into / SESSION_SCRIPT.name


def edit_agc(scripts: Path) -> None:
    """A real change to an included file: AGC's default target, -16 to -16.5 LUFS."""
    agc = scripts / "agc.liq"
    text = agc.read_text(encoding="utf-8")
    assert "~target=-16.," in text
    agc.write_text(text.replace("~target=-16.,", "~target=-16.5,"), encoding="utf-8")


async def test_a_session_starts_after_an_included_script_changes(
    tmp_path: Path, liquidsoap_exe: Path, job: object
) -> None:
    assets = ensure_stream_assets("ffmpeg", tmp_path / "assets")
    engine = EngineConfig(
        exe=liquidsoap_exe,
        script=copy_scripts(tmp_path / "scripts"),
        cache_dir=tmp_path / "cache",
        filler=assets.filler,
        intro_sfx=None,
    )  # D37's 5 s readiness deadline, as in production
    cache = ScriptCache(partial(warm_script_cache, os.environ, engine), engine.script)
    await cache.ensure_warm()  # what the app does at startup
    edit_agc(tmp_path / "scripts")

    item = StubItem(path=long_path(assets.filler), span_s=10.0, title="Filler")
    stub = StubSession(session_id="t1", token=secrets.token_hex(8), items=(item,))
    server, _ = start_stub(stub)
    endpoint = SessionEndpoint(
        session_id=stub.session_id,
        harbor_port=free_port(),
        backend_url=stub_base_url(server, stub.session_id),
        session_token=stub.token,
        log_path=tmp_path / "engine.log",
    )
    start = warm_first(
        cache,
        partial(start_ready_engine, job.assign, os.environ, engine=engine),  # type: ignore[attr-defined]
    )
    try:
        running = await start(endpoint)
        running.stop()
        running.upstream.close()
    finally:
        server.shutdown()
