"""Engine item carried from PR A, run live (slow): a minimal base_env (traceability C8).

A session given only session_base_env(os.environ) still plays: the allow-list is enough for
Liquidsoap to start, reach its backend and serve its harbor.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from stream_stub import (
    StubLog,
    StubSession,
    first_audio,
    free_port,
    start_stub,
    stub_base_url,
)

from backend.playout.assets import ensure_stream_assets
from backend.playout.liquidsoap_process import (
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    session_base_env,
    start_session,
)
from tests.playout.test_session_liq_errors import (
    FIRST_AUDIO_TIMEOUT_S,
    SessionSetup,
    job,  # noqa: F401 - the fixture, imported so pytest finds it
    rs_trace,
    stub_session,
    tone_items,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]


@dataclass(frozen=True)
class Running:
    log: StubLog
    port: int
    began: float
    engine_log: Path


@contextmanager
def session_with_env(
    job: object,  # noqa: F811 - the fixture's value, passed in
    stub: StubSession,
    setup: SessionSetup,
    env: Mapping[str, str],
) -> Iterator[Running]:
    server, log = start_stub(stub)
    process = None
    try:
        engine = EngineConfig(
            exe=setup.exe,
            script=SESSION_SCRIPT,
            cache_dir=setup.cache,
            filler=ensure_stream_assets("ffmpeg", setup.work / "assets").filler,
            intro_sfx=setup.intro,
        )
        endpoint = SessionEndpoint(
            session_id=stub.session_id,
            harbor_port=free_port(),
            backend_url=stub_base_url(server, stub.session_id),
            session_token=stub.token,
            log_path=setup.work / "engine.log",
        )
        began = time.monotonic()
        process = start_session(job.assign, env, endpoint, engine)  # type: ignore[attr-defined]
        yield Running(log, endpoint.harbor_port, began, endpoint.log_path)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        server.shutdown()
        server.server_close()


def test_a_session_with_only_the_allow_listed_environment_plays(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,  # noqa: F811
) -> None:
    stub = stub_session(tone_items(tmp_path, [12.0, 12.0]))
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    with session_with_env(job, stub, setup, session_base_env(os.environ)) as running:
        _, response = first_audio(running.port, running.began, FIRST_AUDIO_TIMEOUT_S)
        try:
            started_at = running.log.started.wait_for(0, timeout_s=10)
        finally:
            response.close()
        trace = rs_trace(running.engine_log)
    assert started_at > running.began, trace
