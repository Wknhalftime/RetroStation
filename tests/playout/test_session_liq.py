"""session.liq against the real Liquidsoap 2.4.5 and a stub item API (spec: Engine).

Timing facts these bounds rest on (spike, 2026-09-27): a start-next push lands 0-120 ms
late; the filler is 2 s long and each of its track boundaries retries the backend.
"""

from __future__ import annotations

import array
import math
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")

from stream_stub import (  # noqa: E402
    StubItem,
    StubLog,
    StubSession,
    ToneSpec,
    first_audio,
    free_port,
    make_tone,
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

START_NEXT_S = 4.0  # sn_rem sent by stream_stub.item_annotations
FIRST_AUDIO_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class SessionSetup:
    exe: Path
    cache: Path
    work: Path
    intro: Path | None
    no_client_exit_s: float = 15.0


@dataclass(frozen=True)
class StubbedSession:
    process: subprocess.Popen[bytes]
    log: StubLog
    started_at: float
    port: int
    engine_log: Path


@pytest.fixture
def job() -> Iterator[object]:
    with windows_job.KillOnCloseJob() as owned:
        yield owned


def setup_for(exe: Path, cache: Path, work: Path) -> SessionSetup:
    return SessionSetup(exe=exe, cache=cache, work=work, intro=None)


def tones(work: Path, spans: list[float]) -> list[StubItem]:
    return [
        StubItem(
            path=long_path(make_tone("ffmpeg", work / f"t{i}.flac", ToneSpec(400 + 100 * i, span))),
            span_s=span,
            title=f"Tone {i}",
        )
        for i, span in enumerate(spans)
    ]


def stub_session(items: list[StubItem], hold_from: int | None = None) -> StubSession:
    return StubSession(
        session_id="t1", token=secrets.token_hex(8), items=tuple(items), hold_from=hold_from
    )


def start_stubbed_session(job: object, stub: StubSession, setup: SessionSetup) -> StubbedSession:
    server, log = start_stub(stub)
    assets = ensure_stream_assets("ffmpeg", setup.work / "assets")
    port = free_port()
    engine = EngineConfig(
        exe=setup.exe,
        script=SESSION_SCRIPT,
        cache_dir=setup.cache,
        filler=assets.filler,
        intro_sfx=setup.intro,
        no_client_exit_s=setup.no_client_exit_s,
    )
    endpoint = SessionEndpoint(
        session_id=stub.session_id,
        harbor_port=port,
        backend_url=stub_base_url(server, stub.session_id),
        session_token=stub.token,
        log_path=setup.work / "engine.log",
    )
    started_at = time.monotonic()
    process = start_session(job.assign, os.environ, endpoint, engine)  # type: ignore[attr-defined]
    return StubbedSession(process, log, started_at, port, endpoint.log_path)


def garbage_file(work: Path) -> Path:
    bad = work / "garbage.flac"
    bad.write_bytes(b"fLaC" + secrets.token_bytes(20_000))
    return bad


def missing_file(work: Path) -> Path:
    return work / "no-such-file.flac"


BAD_FILES = {"missing": missing_file, "garbage": garbage_file}


def levels_db(mp3: Path, window_s: float = 0.1) -> list[float]:
    """RMS level in dBFS of each ``window_s`` slice of the decoded recording (mono)."""
    rate = 8000
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    samples = array.array("h", pcm)
    size = int(rate * window_s)
    out: list[float] = []
    for start in range(0, len(samples) - size + 1, size):
        chunk = samples[start : start + size]
        mean_square = sum(s * s for s in chunk) / size
        out.append(-120.0 if mean_square == 0 else 10 * math.log10(mean_square / 32768**2))
    return out


def longest_run_s(levels: list[float], low: float, high: float, window_s: float = 0.1) -> float:
    best = run = 0
    for level in levels:
        run = run + 1 if low <= level < high else 0
        best = max(best, run)
    return best * window_s


def band_peak_db(mp3: Path, frequency: int) -> float:
    band = f"bandpass=f={frequency}:width_type=q:width=10"
    stderr = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(mp3),
            "-af",
            f"{band},{band},volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    ).stderr
    return _max_volume(stderr)


def peak_db(mp3: Path) -> float:
    stderr = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(mp3),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    ).stderr
    return _max_volume(stderr)


def _max_volume(ffmpeg_stderr: str) -> float:
    match = re.search(r"max_volume: (-?[0-9.]+) dB", ffmpeg_stderr)
    assert match, ffmpeg_stderr
    return float(match.group(1))


def test_plays_items_in_order_with_intro(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    intro = ensure_stream_assets("ffmpeg", tmp_path / "assets").static_intro
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, intro)
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [12, 12, 12])), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    assert response.getheader("Content-Type") == "audio/mpeg"
    streamed = record_for(response, 20, tmp_path / "out.mp3")
    response.close()
    assert streamed >= 19.5
    assert (tmp_path / "out.mp3").stat().st_size > 150_000
    started = session.log.started.seqs()
    assert started[:2] == [0, 1]
    assert len(started) == len(set(started))
    session.process.wait(timeout=10)


def test_session_loads_script_from_warm_cache(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    setup = setup_for(liquidsoap_exe, liq_cache, tmp_path)
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [12])), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    response.close()
    session.process.wait(timeout=10)
    assert "Loading main script from cache" in session.engine_log.read_text(errors="replace")


@pytest.mark.parametrize("make_intro", BAD_FILES.values(), ids=BAD_FILES.keys())
def test_bad_intro_still_plays(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object, make_intro: object
) -> None:
    intro = make_intro(tmp_path)  # type: ignore[operator]
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, intro)
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [12, 12])), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    record_for(response, 8, tmp_path / "out.mp3")
    response.close()
    assert session.log.started.seqs()[:1] == [0]
    session.process.wait(timeout=10)


def test_end_of_schedule_closes_stream(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    setup = setup_for(liquidsoap_exe, liq_cache, tmp_path)
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [10, 10])), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    # Two items overlap by START_NEXT_S: 16 s of audio, then the filler ends the session.
    streamed = record_for(response, 60, tmp_path / "out.mp3")
    assert streamed < 30, "the stream did not reach EOF after the last item"
    assert streamed >= 2 * 10 - START_NEXT_S - 1.5, "the last item was cut short"
    assert session.log.started.seqs() == [0, 1]
    assert 2 in session.log.requested.seqs()
    session.process.wait(timeout=10)


@pytest.mark.parametrize("make_bad", BAD_FILES.values(), ids=BAD_FILES.keys())
def test_unreadable_item_is_reported_and_skipped_without_gap(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object, make_bad: object
) -> None:
    items = tones(tmp_path, [12, 12, 12])
    bad = make_bad(tmp_path)  # type: ignore[operator]
    items.insert(1, StubItem(path=long_path(bad), span_s=12, title="Bad"))
    setup = setup_for(liquidsoap_exe, liq_cache, tmp_path)
    session = start_stubbed_session(job, stub_session(items), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    record_for(response, 14, tmp_path / "out.mp3")
    response.close()
    assert session.log.failed.seqs() == [1]
    assert session.log.started.seqs()[:2] == [0, 2]
    starts = dict(zip(session.log.started.seqs(), session.log.started.times(), strict=True))
    assert starts[2] - starts[0] == pytest.approx(12 - START_NEXT_S, abs=1.0)
    session.process.wait(timeout=10)


def test_exits_when_no_client_connects(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None, no_client_exit_s=3.0)
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [12])), setup)
    session.process.wait(timeout=15)


def test_connected_client_keeps_session_alive_and_disconnect_ends_it(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    no_client_exit_s = 4.0
    setup = SessionSetup(
        liquidsoap_exe, liq_cache, tmp_path, None, no_client_exit_s=no_client_exit_s
    )
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [30])), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    record_for(response, no_client_exit_s + 3, tmp_path / "out.mp3")
    assert session.process.poll() is None, "session exited while a client was connected"
    response.close()
    session.process.wait(timeout=3)


def test_harbor_listens_on_loopback_only(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    setup = setup_for(liquidsoap_exe, liq_cache, tmp_path)
    session = start_stubbed_session(job, stub_session(tones(tmp_path, [12])), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    listening = [
        c.laddr
        for c in psutil.Process(session.process.pid).net_connections("inet")
        if c.status == psutil.CONN_LISTEN
    ]
    response.close()
    assert session.port in {addr.port for addr in listening}
    assert {addr.ip for addr in listening} == {"127.0.0.1"}
    session.process.wait(timeout=10)


def test_backend_outage_plays_filler_then_resumes(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    setup = setup_for(liquidsoap_exe, liq_cache, tmp_path)
    stub = stub_session(tones(tmp_path, [12, 12, 12, 12]), hold_from=2)
    session = start_stubbed_session(job, stub, setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    recording = tmp_path / "out.mp3"
    stop = threading.Event()
    recorder = threading.Thread(target=lambda: record_for(response, 90, recording, stop))
    recorder.start()
    try:
        item_1_at = session.log.started.wait_for(1, timeout_s=30)
        # Item 1 ends 12 s after it starts; let the filler run for 4 s after that.
        time.sleep(max(0.0, item_1_at + 12 + 4 - time.monotonic()))
        assert 2 not in session.log.started.seqs()
        released_at = time.monotonic()
        session.log.released.set()
        item_2_at = session.log.started.wait_for(2, timeout_s=10)
        time.sleep(2)
    finally:
        stop.set()
        recorder.join(timeout=10)
        response.close()
    assert item_2_at - released_at <= 4.0
    levels = levels_db(recording)
    # The filler (about -57 dBFS RMS) fills the outage: quiet, but never dead air.
    assert longest_run_s(levels, low=-75.0, high=-30.0) >= 2.0
    assert longest_run_s(levels, low=-200.0, high=-75.0) < 0.5
    session.process.wait(timeout=10)


def test_gain_is_applied_and_output_peak_is_limited(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    def tone(frequency: int, gain_db: float) -> StubItem:
        path = make_tone("ffmpeg", tmp_path / f"g{frequency}.flac", ToneSpec(frequency, 12))
        return StubItem(path=long_path(path), span_s=12, title=f"{frequency} Hz", gain_db=gain_db)

    # Tones are -12 dBFS. -20/-10 dB stays under the compressor threshold (-18 dBFS);
    # +18 dB drives the chain 6 dB over full scale.
    items = [tone(500, -20.0), tone(1500, -10.0), tone(3000, 18.0)]
    setup = setup_for(liquidsoap_exe, liq_cache, tmp_path)
    session = start_stubbed_session(job, stub_session(items), setup)
    _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
    recording = tmp_path / "out.mp3"
    record_for(response, 3 * 12 - 2 * START_NEXT_S + 2, recording)
    response.close()
    quiet, louder = band_peak_db(recording, 500), band_peak_db(recording, 1500)
    assert quiet == pytest.approx(-32.0, abs=1.5)
    assert louder - quiet == pytest.approx(10.0, abs=1.0)
    assert peak_db(recording) <= -0.5
    session.process.wait(timeout=10)
