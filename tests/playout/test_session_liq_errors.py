"""session.liq error paths against the real Liquidsoap 2.4.5 (spec: Errors and edge cases).

A small HTTP proxy sits between Liquidsoap and the frozen stub item API
(``scripts/stream_stub.py``): it delays or rejects calls, and the stub stays unchanged.

Timing facts these bounds rest on (spike, 2026-09-27): a start-next push lands 0-120 ms
late; the filler is ``FILLER.duration_s`` (2 s) long and each of its track boundaries
retries the backend.
"""

from __future__ import annotations

import array
import contextlib
import http.client
import math
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")

from stream_stub import (  # noqa: E402
    SeqLog,
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

from backend.playout.assets import FILLER, ensure_stream_assets  # noqa: E402
from backend.playout.liquidsoap_process import (  # noqa: E402
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    long_path,
    start_session,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

START_NEXT_S = 4.0  # sn_rem sent by stream_stub.item_annotations
FILLER_PERIOD_S = FILLER.duration_s  # each filler track boundary retries the backend
INTRO_FADE_AT_S = 1.0  # EngineConfig default: the first song starts this far into the intro
FIRST_AUDIO_TIMEOUT_S = 15.0
LEVEL_WINDOW_S = 0.1
LOUD_DB = -35.0  # an item's own band sits near -15 dBFS RMS; a neighbour leaks in below -50
# Neighbours differ by a factor of 1.45: two bandpass(Q=10) passes reject a neighbour by
# about 35 dB, so each band shows only its own item.
FREQUENCIES = (300, 435, 630, 915, 1325, 1920, 2790, 4045)


@dataclass(frozen=True)
class SessionSetup:
    exe: Path
    cache: Path
    work: Path
    intro: Path | None


@dataclass(frozen=True)
class ProxyRules:
    """How the proxy bends the backend.

    ``item_delay_s``: every item GET is answered this late.
    ``stall_seq``/``stall_s``: the first GET of that seq is answered ``stall_s`` late instead.
    ``started_status``: POST .../started is answered with this status and not forwarded.
    """

    item_delay_s: float = 0.0
    stall_seq: int | None = None
    stall_s: float = 0.0
    started_status: int | None = None


@dataclass(frozen=True)
class ProxyLog:
    rejected_started: SeqLog = field(default_factory=SeqLog)


@dataclass(frozen=True)
class ProxiedSession:
    process: subprocess.Popen[bytes]
    stub: StubLog
    proxy: ProxyLog
    started_at: float
    port: int
    engine_log: Path


@pytest.fixture
def job() -> Iterator[object]:
    with windows_job.KillOnCloseJob() as owned:
        yield owned


_ITEM_ROUTE = re.compile(r"/items/(\d+)(/[a-z]+)?$")


def _proxy_handler(
    upstream_port: int, rules: ProxyRules, log: ProxyLog
) -> type[BaseHTTPRequestHandler]:
    stalled: set[int] = set()
    stalled_lock = threading.Lock()

    def delay_for(seq: int) -> float:
        with stalled_lock:
            if seq == rules.stall_seq and seq not in stalled:
                stalled.add(seq)
                return rules.stall_s
        return rules.item_delay_s

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _forward(self, method: str) -> tuple[int, bytes]:
            upstream = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=10)
            try:
                upstream.request(
                    method,
                    self.path,
                    body=b"" if method == "POST" else None,
                    headers={"X-Session-Token": self.headers.get("X-Session-Token", "")},
                )
                response = upstream.getresponse()
                return response.status, response.read()
            finally:
                upstream.close()

        def _reply(self, status: int, body: bytes) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                return  # Liquidsoap gave up on a stalled answer and closed the socket

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            status, body = self._forward("GET")
            route = _ITEM_ROUTE.search(self.path)
            if route and route.group(2) is None:
                time.sleep(delay_for(int(route.group(1))))
            self._reply(status, body)

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            route = _ITEM_ROUTE.search(self.path)
            if rules.started_status is not None and route and route.group(2) == "/started":
                log.rejected_started.record(int(route.group(1)))
                return self._reply(rules.started_status, b"")
            self._reply(*self._forward("POST"))

    return Handler


def _serve(server: ThreadingHTTPServer) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


@contextmanager
def proxied_session(
    job: object, stub: StubSession, setup: SessionSetup, rules: ProxyRules
) -> Iterator[ProxiedSession]:
    """Liquidsoap -> proxy -> stub. On exit the session, proxy and stub are all stopped."""
    stub_server, stub_log = start_stub(stub)
    proxy_log = ProxyLog()
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0), _proxy_handler(stub_server.server_port, rules, proxy_log)
    )
    _serve(proxy)
    process: subprocess.Popen[bytes] | None = None
    try:
        assets = ensure_stream_assets("ffmpeg", setup.work / "assets")
        engine = EngineConfig(
            exe=setup.exe,
            script=SESSION_SCRIPT,
            cache_dir=setup.cache,
            filler=assets.filler,
            intro_sfx=setup.intro,
            intro_fade_at_s=INTRO_FADE_AT_S,
        )
        stub_url = stub_base_url(stub_server, stub.session_id)
        endpoint = SessionEndpoint(
            session_id=stub.session_id,
            harbor_port=free_port(),
            backend_url=stub_url.replace(
                f":{stub_server.server_port}/", f":{proxy.server_port}/", 1
            ),
            session_token=stub.token,
            log_path=setup.work / "engine.log",
        )
        started_at = time.monotonic()
        process = start_session(job.assign, os.environ, endpoint, engine)  # type: ignore[attr-defined]
        yield ProxiedSession(
            process, stub_log, proxy_log, started_at, endpoint.harbor_port, endpoint.log_path
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        _stop(proxy)
        _stop(stub_server)


def tone_items(work: Path, spans: list[float]) -> list[StubItem]:
    return [
        StubItem(
            path=long_path(
                make_tone("ffmpeg", work / f"t{i}.flac", ToneSpec(FREQUENCIES[i], span))
            ),
            span_s=span,
            title=f"Tone {i}",
        )
        for i, span in enumerate(spans)
    ]


def stub_session(items: list[StubItem]) -> StubSession:
    return StubSession(session_id="t1", token=secrets.token_hex(8), items=tuple(items))


@contextmanager
def listening(response: http.client.HTTPResponse, out: Path) -> Iterator[None]:
    """Keep reading the stream in the background, as a player would, until the block ends."""
    stop = threading.Event()
    reader = threading.Thread(target=record_for, args=(response, 150, out, stop))
    reader.start()
    try:
        yield
    finally:
        stop.set()
        reader.join(timeout=10)
        response.close()


def await_seq(log: SeqLog, seq: int, deadline: float) -> None:
    """Wait until ``seq`` is recorded or ``deadline`` (time.monotonic()) passes."""
    with contextlib.suppress(TimeoutError):
        log.wait_for(seq, timeout_s=max(0.0, deadline - time.monotonic()))


# ---- engine log -------------------------------------------------------------------------


def engine_lines(log: Path, event: str | None = None) -> list[str]:
    """``RS`` lines the script printed, optionally only those of one event."""
    text = log.read_text(errors="replace") if log.exists() else ""
    prefix = "RS " if event is None else f"RS {event} "
    return [line for line in text.splitlines() if line.startswith(prefix)]


def await_engine_line(
    log: Path, event: str, wanted: Callable[[str], bool], timeout_s: float
) -> str | None:
    """The first ``RS <event>`` line ``wanted`` accepts, waiting up to ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while True:
        found = next((line for line in engine_lines(log, event) if wanted(line)), None)
        if found is not None or time.monotonic() >= deadline:
            return found
        time.sleep(0.02)


def rs_trace(log: Path) -> str:
    return "\n".join(engine_lines(log))


# ---- audio ------------------------------------------------------------------------------


def band_levels_db(mp3: Path, frequency: int) -> list[float]:
    """RMS dBFS of each LEVEL_WINDOW_S slice of the recording, band-passed at ``frequency``."""
    rate = 22050
    band = f"bandpass=f={frequency}:width_type=q:width=10"
    pcm = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(mp3),
            "-af",
            f"{band},{band}",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    samples = array.array("h", pcm)
    size = int(rate * LEVEL_WINDOW_S)
    out: list[float] = []
    for start in range(0, len(samples) - size + 1, size):
        chunk = samples[start : start + size]
        mean_square = sum(s * s for s in chunk) / size
        out.append(-120.0 if mean_square == 0 else 10 * math.log10(mean_square / 32768**2))
    return out


def loud_s(levels: list[float]) -> float:
    return sum(1 for level in levels if level >= LOUD_DB) * LEVEL_WINDOW_S


def co_loud_s(a: list[float], b: list[float]) -> float:
    """Seconds during which both bands are loud at once."""
    both = sum(1 for x, y in zip(a, b, strict=True) if x >= LOUD_DB and y >= LOUD_DB)
    return both * LEVEL_WINDOW_S


# ---- 1. backend slow (spec row "Backend slow"; review finding I1) --------------------------

# Every item answer takes SLOW_DELAY_S, longer than one filler loop, and the first answer
# for STALL_SEQ takes STALL_S, longer than session.liq's 10 s fetch timeout. The session
# plays its 2 prefetched items, then the filler, and resumes when the backend answers again.
# The resume is fetched from a filler retry. When that retry's fetch loop ends it must not
# push a second item while one is already playing (finding I1).
# SLOW_SPAN_S keeps items 6 s apart, so only neighbours ever legitimately overlap.
SLOW_DELAY_S = 3.0
SLOW_SPAN_S = 10.0
SLOW_ITEMS = 6
STALL_SEQ = 2
STALL_S = 12.0
# Two decks legitimately overlap for the start-next window plus the push latency; fades
# make the audibly overlapping part shorter still.
MAX_CROSSFADE_S = START_NEXT_S + 0.5


def test_slow_backend_plays_every_item_once_in_order_without_overlap(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    items = tone_items(tmp_path, [SLOW_SPAN_S] * SLOW_ITEMS)
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    rules = ProxyRules(item_delay_s=SLOW_DELAY_S, stall_seq=STALL_SEQ, stall_s=STALL_S)
    recording = tmp_path / "out.mp3"
    with proxied_session(job, stub_session(items), setup, rules) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        # The schedule ends after SLOW_ITEMS items and the stream closes (end of schedule).
        record_for(response, 120, recording)
        response.close()
        started = session.stub.started.seqs()
        trace = rs_trace(session.engine_log)
    levels = [band_levels_db(recording, frequency) for frequency in FREQUENCIES[:SLOW_ITEMS]]
    overlaps = {
        (i, j): co_loud_s(levels[i], levels[j])
        for i in range(SLOW_ITEMS)
        for j in range(i + 1, SLOW_ITEMS)
    }
    too_long = {
        pair: seconds
        for pair, seconds in overlaps.items()
        if seconds > (MAX_CROSSFADE_S if pair[1] == pair[0] + 1 else LEVEL_WINDOW_S)
    }
    assert not too_long, f"songs played at once (item pair: seconds): {too_long}\n{trace}"
    assert started == list(range(SLOW_ITEMS)), f"started {started}\n{trace}"
    heard = [loud_s(band) for band in levels]
    assert min(heard) >= SLOW_SPAN_S - 2.0, f"an item was cut short: {heard}\n{trace}"


# ---- 2. file missing or unreadable at play time (spec row; review finding I2) --------------

ITEM_SPAN_S = 12.0
# The next deck must take over at item 0's start-next point, as if item 1 had not been
# scheduled. One filler period of slack allows a fix that retries from the filler boundary,
# plus 1 s for the push latency and the failed POST.
SKIP_BOUND_S = ITEM_SPAN_S - START_NEXT_S + FILLER_PERIOD_S + 1.0


# A fixed pattern, not random bytes, so every run feeds the decoder the same garbage.
GARBAGE = bytes(i * 7 % 251 for i in range(20_000))


def overwrite_with_garbage(path: Path) -> None:
    path.write_bytes(b"fLaC" + GARBAGE)


def empty_file(path: Path) -> None:
    path.write_bytes(b"")


# Deleting is not among them: from its resolve until it plays, Liquidsoap holds the file
# open, and Windows refuses to delete an open file (WinError 32). A file on local disk
# cannot go missing in that window; it can only change under the open handle.
LOSSES = {"overwritten": overwrite_with_garbage, "emptied": empty_file}


@pytest.mark.parametrize("lose", LOSSES.values(), ids=LOSSES.keys())
def test_item_lost_after_prefetch_is_reported_and_skipped(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,
    lose: Callable[[Path], None],
) -> None:
    items = tone_items(tmp_path, [ITEM_SPAN_S] * 3)
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    with proxied_session(job, stub_session(items), setup, ProxyRules()) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        with listening(response, tmp_path / "out.mp3"):
            # RS FETCHED is printed once the item has passed the content-type resolve.
            fetched = await_engine_line(
                session.engine_log, "FETCHED", lambda line: " seq=1 " in line, timeout_s=10
            )
            assert fetched, f"harness: item 1 never fetched\n{rs_trace(session.engine_log)}"
            item_0_at = session.stub.started.wait_for(0, timeout_s=10)
            lose(tmp_path / "t1.flac")
            assert time.monotonic() < item_0_at + ITEM_SPAN_S - START_NEXT_S - 1.0, (
                "harness: the file was lost too close to its deck's start"
            )
            deadline = item_0_at + SKIP_BOUND_S + 1.0
            await_seq(session.stub.started, 2, deadline)
            await_seq(session.stub.failed, 1, deadline)
        failed, started = session.stub.failed.seqs(), session.stub.started.seqs()
        starts = dict(zip(started, session.stub.started.times(), strict=True))
        trace = rs_trace(session.engine_log)
    assert failed == [1], f"failed POSTs {failed}, started {started}\n{trace}"
    assert started[:2] == [0, 2], f"started {started}\n{trace}"
    assert starts[2] - item_0_at <= SKIP_BOUND_S, f"item 2 late\n{trace}"
    assert starts[2] - item_0_at >= ITEM_SPAN_S - START_NEXT_S - 1.0, f"item 2 early\n{trace}"


def flac_audio_offset(flac: bytes) -> int:
    """Where the audio frames begin: after the "fLaC" marker and every metadata block."""
    assert flac[:4] == b"fLaC"
    offset = 4
    while True:
        header, length = flac[offset], int.from_bytes(flac[offset + 1 : offset + 4], "big")
        offset += 4 + length
        if header & 0x80:  # last-metadata-block flag
            return offset


def intro_broken_after_header(work: Path) -> Path:
    """A real intro whose headers are intact but whose audio frames are zeroed.

    It passes ``request.resolve(content_type=...)`` (the header is valid) but yields no
    audio when played: the intro resolves, then fails at play time.
    """
    good = ensure_stream_assets("ffmpeg", work / "assets").static_intro.read_bytes()
    offset = flac_audio_offset(good)
    broken = work / "broken_intro.flac"
    broken.write_bytes(good[:offset] + bytes(len(good) - offset))
    return broken


def test_intro_that_fails_at_play_time_still_starts_first_song(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    intro = intro_broken_after_header(tmp_path)
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, intro)
    items = tone_items(tmp_path, [ITEM_SPAN_S] * 2)
    # The first song is due INTRO_FADE_AT_S into the intro. Allow one filler period for a
    # retry from the filler boundary, plus 1 s of slack. It is counted from first audio: by
    # then the output, and so the intro, has started.
    bound_s = INTRO_FADE_AT_S + FILLER_PERIOD_S + 1.0
    with proxied_session(job, stub_session(items), setup, ProxyRules()) as session:
        waited, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        first_audio_at = session.started_at + waited
        with listening(response, tmp_path / "out.mp3"):
            await_seq(session.stub.started, 0, first_audio_at + bound_s + 1.0)
        started = session.stub.started.seqs()
        times = session.stub.started.times()
        trace = rs_trace(session.engine_log)
    assert "RS INTRO " in trace, f"harness: the broken intro did not resolve\n{trace}"
    assert started[:1] == [0], f"the first song never started: {started}\n{trace}"
    assert times[0] - first_audio_at <= bound_s, f"first song late\n{trace}"


# ---- 3. failed POSTs are visible (review finding I4; rule "never skip error handling") ----


def test_rejected_started_post_is_logged_and_playback_continues(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    items = tone_items(tmp_path, [ITEM_SPAN_S] * 2)
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    rules = ProxyRules(started_status=500)

    def reports_500_for(seq: int) -> Callable[[str], bool]:
        route = re.compile(rf"/items/{seq}/started\b")
        return lambda line: bool(route.search(line) and re.search(r"\b500\b", line))

    with proxied_session(job, stub_session(items), setup, rules) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        with listening(response, tmp_path / "out.mp3"):
            # Item 1 starts at item 0's start-next point, ITEM_SPAN_S - START_NEXT_S in.
            await_seq(session.proxy.rejected_started, 1, time.monotonic() + ITEM_SPAN_S)
            # The POST's own timeout is 5 s; the line follows the 500 within that.
            logged = [
                await_engine_line(session.engine_log, "POST_FAILED", reports_500_for(seq), 5.0)
                for seq in (0, 1)
            ]
        rejected = session.proxy.rejected_started.seqs()
        trace = rs_trace(session.engine_log)
    assert rejected[:2] == [0, 1], f"playback stopped: started POSTs {rejected}\n{trace}"
    assert all(logged), f"POST_FAILED lines with status 500 for seqs 0, 1: {logged}\n{trace}"
