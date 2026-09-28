"""session.liq deck rotation with short items (spec D7, D9; Errors table).

Decks rotate A -> B -> C, so the push for item k lands on the deck item k-3 used. After a
long segue (a large ``sn_rem``) followed by short items, that deck can still be playing
when the push arrives. The pushed item must then wait its turn: it is healthy and must not
be reported failed, skipped or played on top of another song.

A proxy in front of the frozen stub (``scripts/stream_stub.py``) rewrites each item's
``sn_rem`` and fades, because the stub always sends ``sn_rem=4``. Fades fit every span (D9).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")

from stream_stub import (  # noqa: E402
    StubLog,
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
    start_session,
)
from tests.playout.test_session_liq_errors import (  # noqa: E402
    FIRST_AUDIO_TIMEOUT_S,
    FREQUENCIES,
    SessionSetup,
    await_engine_line,
    band_levels_db,
    co_loud_s,
    loud_s,
    overwrite_with_garbage,
    rs_trace,
    stub_session,
    tone_items,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

# Two songs may be audible together only where the schedule overlaps them. The slack covers
# the push latency (up to 0.12 s, spike) plus one 0.1 s level window at each end.
OVERLAP_SLACK_S = 0.5


@dataclass(frozen=True)
class Cue:
    """One item's playable span and the annotations the proxy sends for it (D9)."""

    span_s: float
    sn_rem_s: float
    fade_in_s: float
    fade_out_s: float

    def annotations(self) -> dict[str, str]:
        return {
            "sn_rem": f"{self.sn_rem_s:.3f}",
            "liq_fade_in": f"{self.fade_in_s:.3f}",
            "liq_fade_out": f"{self.fade_out_s:.3f}",
        }


# A song with a 10 s segue, then two 4 s jingles 3 s apart: the push after the second
# jingle comes 6 s into the segue, while the song still plays on that deck (6 < 10).
SEGUE = Cue(span_s=20.0, sn_rem_s=10.0, fade_in_s=3.0, fade_out_s=8.0)
JINGLE = Cue(span_s=4.0, sn_rem_s=1.0, fade_in_s=0.3, fade_out_s=0.5)
SONG = Cue(span_s=12.0, sn_rem_s=4.0, fade_in_s=3.0, fade_out_s=4.0)
D9_MARGIN_S = 3.0


@pytest.fixture
def job() -> Iterator[object]:
    with windows_job.KillOnCloseJob() as owned:
        yield owned


_ITEM_GET = re.compile(r"/items/(\d+)$")


def _cue_rewriter(upstream_port: int, cues: Mapping[int, Cue]) -> type[BaseHTTPRequestHandler]:
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
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            status, body = self._forward("GET")
            route = _ITEM_GET.search(self.path)
            if status == 200 and route and int(route.group(1)) in cues:
                item = json.loads(body)
                item["annotations"].update(cues[int(route.group(1))].annotations())
                body = json.dumps(item).encode()
            self._reply(status, body)

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(*self._forward("POST"))

    return Handler


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


@dataclass(frozen=True)
class CuedSession:
    process: subprocess.Popen[bytes]
    stub: StubLog
    started_at: float
    port: int
    engine_log: Path


@contextmanager
def cued_session(
    job: object, stub: StubSession, setup: SessionSetup, cues: Mapping[int, Cue]
) -> Iterator[CuedSession]:
    """Liquidsoap -> cue-rewriting proxy -> stub. On exit all three are stopped."""
    stub_server, stub_log = start_stub(stub)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _cue_rewriter(stub_server.server_port, cues))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    process: subprocess.Popen[bytes] | None = None
    try:
        engine = EngineConfig(
            exe=setup.exe,
            script=SESSION_SCRIPT,
            cache_dir=setup.cache,
            filler=ensure_stream_assets("ffmpeg", setup.work / "assets").filler,
            intro_sfx=setup.intro,
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
        yield CuedSession(process, stub_log, started_at, endpoint.harbor_port, endpoint.log_path)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        _stop(proxy)
        _stop(stub_server)


def scheduled_intervals(played: list[Cue]) -> list[tuple[float, float]]:
    """(start, end) of each played item if every one starts at its predecessor's start-next.

    Anything that starts later only shortens the overlaps, so these are upper bounds.
    """
    out: list[tuple[float, float]] = []
    start = 0.0
    for cue in played:
        out.append((start, start + cue.span_s))
        start += cue.span_s - cue.sn_rem_s
    return out


def band_levels(recording: Path, seqs: list[int]) -> dict[int, list[float]]:
    """Each item's level track: ``tone_items`` gives item ``seq`` FREQUENCIES[seq]."""
    return {seq: band_levels_db(recording, FREQUENCIES[seq]) for seq in seqs}


def overlap_violations(
    levels: dict[int, list[float]], played: dict[int, Cue]
) -> dict[tuple[int, int], str]:
    """Item pairs audible together for longer than the schedule overlaps them."""
    seqs = sorted(played)
    windows = dict(zip(seqs, scheduled_intervals([played[s] for s in seqs]), strict=True))
    found: dict[tuple[int, int], str] = {}
    for n, a in enumerate(seqs):
        for b in seqs[n + 1 :]:
            (a0, a1), (b0, b1) = windows[a], windows[b]
            allowed = max(0.0, min(a1, b1) - max(a0, b0)) + OVERLAP_SLACK_S
            heard = co_loud_s(levels[a], levels[b])
            if heard > allowed:
                found[(a, b)] = f"{heard:.1f} s together, schedule allows {allowed:.1f} s"
    return found


def cut_short(levels: dict[int, list[float]], played: dict[int, Cue]) -> dict[int, float]:
    """Items loud for clearly less than their span.

    A linear fade stays above LOUD_DB for about 90 % of its length. When the compressor
    ducks a fading item under a louder one it drops out sooner, so a quarter of each fade
    plus 0.5 s may go unheard.
    """
    heard = {seq: loud_s(levels[seq]) for seq in played}
    return {
        seq: s
        for seq, s in heard.items()
        if s < played[seq].span_s - 0.25 * (played[seq].fade_in_s + played[seq].fade_out_s) - 0.5
    }


def check_d9(cues: Mapping[int, Cue]) -> None:
    for seq, cue in cues.items():
        assert cue.span_s >= cue.fade_in_s + cue.fade_out_s + D9_MARGIN_S, f"harness: D9 {seq}"
        assert cue.sn_rem_s < cue.span_s, f"harness: sn_rem {seq}"


def schedule(work: Path, cues: dict[int, Cue]) -> StubSession:
    check_d9(cues)
    return stub_session(tone_items(work, [cues[seq].span_s for seq in sorted(cues)]))


# ---- 1. short items after a long segue (D7; the 1 s never-started check) -----------------


def test_short_items_after_a_long_segue_all_play_once_in_order(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    # Item 3's push comes 16 s in, on item 0's deck, while item 0 plays until 20 s.
    cues = {0: SEGUE, 1: JINGLE, 2: JINGLE, 3: SONG, 4: SONG}
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    recording = tmp_path / "out.mp3"
    with cued_session(job, schedule(tmp_path, cues), setup, cues) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        # The schedule ends after the last item and the stream closes (end of schedule).
        record_for(response, 120, recording)
        response.close()
        failed, started = session.stub.failed.seqs(), session.stub.started.seqs()
        trace = rs_trace(session.engine_log)
    assert failed == [], f"healthy items reported failed: {failed}\n{trace}"
    assert started == sorted(cues), f"started {started}\n{trace}"
    levels = band_levels(recording, sorted(cues))
    overlaps = overlap_violations(levels, cues)
    assert not overlaps, f"songs played at once: {overlaps}\n{trace}"
    short = cut_short(levels, cues)
    assert not short, f"items cut short (loud seconds): {short}\n{trace}"


# ---- 2. two play-time failures during a segue (Errors table: failed -> skipped) ---------


def test_two_play_time_failures_during_a_segue_fail_only_the_broken_items(
    tmp_path: Path, liquidsoap_exe: Path, liq_cache: Path, job: object
) -> None:
    # Items 1 and 2 break after prefetch. Each takes the 1 s never-started check to fail, so
    # item 3 is pushed about 2 s into item 0's 10 s segue, onto item 0's deck.
    cues = {0: SEGUE, 1: SONG, 2: SONG, 3: SONG, 4: SONG}
    broken = [1, 2]
    played = {seq: cue for seq, cue in cues.items() if seq not in broken}
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    recording = tmp_path / "out.mp3"
    with cued_session(job, schedule(tmp_path, cues), setup, cues) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        recorder = threading.Thread(target=record_for, args=(response, 120, recording))
        recorder.start()
        try:
            # RS FETCHED is printed once the item has passed the content-type resolve.
            fetched = await_engine_line(
                session.engine_log, "FETCHED", lambda line: " seq=2 " in line, timeout_s=15
            )
            assert fetched, f"harness: item 2 never fetched\n{rs_trace(session.engine_log)}"
            item_0_at = session.stub.started.wait_for(0, timeout_s=15)
            for seq in broken:
                overwrite_with_garbage(tmp_path / f"t{seq}.flac")
            assert time.monotonic() < item_0_at + SEGUE.span_s - SEGUE.sn_rem_s - 1.0, (
                "harness: the files broke too close to their decks' start"
            )
        finally:
            recorder.join(timeout=120)  # until the schedule ends and the stream closes
            response.close()
        failed, started = session.stub.failed.seqs(), session.stub.started.seqs()
        trace = rs_trace(session.engine_log)
    assert failed == broken, f"failed POSTs {failed}, expected {broken}\n{trace}"
    assert started == sorted(played), f"started {started}\n{trace}"
    levels = band_levels(recording, sorted(played))
    overlaps = overlap_violations(levels, played)
    assert not overlaps, f"songs played at once: {overlaps}\n{trace}"
    short = cut_short(levels, played)
    assert not short, f"items cut short (loud seconds): {short}\n{trace}"
