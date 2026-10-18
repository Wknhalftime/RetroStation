"""session.liq's final limiter holds the stream's peaks under -1 dBFS (the MP3 listeners get).

A compressor with a 1 ms attack and no lookahead let the start of every hit through: these
bursts, driven 18 dB hot, reached the stream at -0.4 to +0.0 dBFS (2026-10-18, three runs).
The MP3 encode itself adds up to 1.5 dB on dense material, so the limiter sits at -2 dBFS
(measured -1.9 to -2.1 dBFS). Abrupt onsets give the limiter a fresh transient every second.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")

from stream_stub import (  # noqa: E402
    StubItem,
    StubSession,
    first_audio,
    free_port,
    record_for,
    start_stub,
    stub_base_url,
)

from backend.playout.assets import ensure_stream_assets  # noqa: E402
from backend.playout.liquidsoap_process import (  # noqa: E402
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    long_path,
    start_session,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

BURSTS_S = 20
FIRST_AUDIO_TIMEOUT_S = 15.0


@pytest.fixture
def job() -> Iterator[object]:
    with windows_job.KillOnCloseJob() as owned:
        yield owned


def make_bursts(out: Path) -> Path:
    """0.6 s of pink noise plus an 80 Hz sine each second, cut in and out without fades."""
    noise = f"anoisesrc=color=pink:amplitude=0.5:seed=1:duration={BURSTS_S}:sample_rate=44100"
    sine = f"sine=frequency=80:duration={BURSTS_S}:sample_rate=44100"
    graph = (
        "[0][1]amix=inputs=2:normalize=0,"
        "volume='if(lt(mod(t,1),0.6),1,0)':eval=frame,"
        "aformat=channel_layouts=stereo"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", noise, "-f", "lavfi", "-i"]
        + [sine, "-filter_complex", graph, "-c:a", "flac", str(out)],
        check=True,
        timeout=60,
    )
    return out


def start_bursts_session(job: object, item: StubItem, exe: Path, cache: Path, work: Path) -> int:
    stub = StubSession(session_id="t1", token=secrets.token_hex(8), items=(item,))
    server, _ = start_stub(stub)
    assets = ensure_stream_assets("ffmpeg", work / "assets")
    port = free_port()
    engine = EngineConfig(
        exe=exe, script=SESSION_SCRIPT, cache_dir=cache, filler=assets.filler, intro_sfx=None
    )
    endpoint = SessionEndpoint(
        session_id=stub.session_id,
        harbor_port=port,
        backend_url=stub_base_url(server, stub.session_id),
        session_token=stub.token,
        log_path=work / "engine.log",
    )
    start_session(job.assign, os.environ, endpoint, engine)  # type: ignore[attr-defined]
    return port


def ffmpeg_summary(mp3: Path, audio_filter: str) -> str:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(mp3), "-af", audio_filter]
        + ["-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=120,
    ).stderr


def peak_db(mp3: Path) -> float:
    stderr = ffmpeg_summary(mp3, "volumedetect")
    match = re.search(r"max_volume: (-?[0-9.]+) dB", stderr)
    assert match, stderr
    return float(match.group(1))


def loudness_lufs(mp3: Path) -> float:
    stderr = ffmpeg_summary(mp3, "ebur128")
    # ebur128 logs a running I: per frame and then its summary; the last one is the summary.
    found = re.findall(r"I:\s+(-?[0-9.]+) LUFS", stderr)
    assert found, stderr
    return float(found[-1])


def test_hot_bursts_leave_the_stream_under_minus_1_dbfs(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    bursts = make_bursts(tmp_path / "bursts.flac")
    item = StubItem(path=long_path(bursts), span_s=BURSTS_S, title="Bursts", gain_db=18.0)
    port = start_bursts_session(job, item, liquidsoap_exe, liq_cache, tmp_path)
    _, response = first_audio(port, time.monotonic(), FIRST_AUDIO_TIMEOUT_S)
    recording = tmp_path / "out.mp3"
    record_for(response, BURSTS_S, recording)
    response.close()
    # Still loud (measured -14.5 to -14.9 LUFS): a limiter that fell back to silence fails here.
    assert loudness_lufs(recording) >= -20.0
    assert peak_db(recording) <= -1.0
