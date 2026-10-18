"""session.liq with a liner shorter than the outgoing song's fade (PR L2, Task 12; LR6;
Review Focus 5): the song fading out, the liner and the next song play on three decks at once,
with no gap, no stall and no wait for a free deck. ``session.liq`` is unchanged: a liner is an
ordinary annotated item.

A proxy in front of the frozen stub (``scripts/stream_stub.py``) serves the liner as the
stream service sends it, through Task 10's ``liner_payload``, and gives both songs D112's zero
fade-in with a 4 s fade-out and a 4 s start-next (the stub always sends a 3 s fade-in). Start
times are the engine's own ``RS STARTED`` lines, so HTTP latency cannot move them; a push
starts within 0.12 s (spike, 2026-09-27). The bound on B's start is the decks test's 0.5 s
slack (``OVERLAP_SLACK_S``), as every ``test_session_liq_*`` test uses; the liner's EOM sits
1.4 s before its Stop, so a liner sent with no overlap (B at its Stop) is 0.9 s outside it.
(The Task 12 brief's 0.8 s liner and 0.15 s bound were ruled out after the L2 test audit, I3:
too little headroom.)

- song A: 20 s, ``liq_cue_out`` 20.000, ``liq_fade_out`` 4.000, ``sn_rem`` 4.000: the liner
  starts at A's EOM, 16 s in, while A fades out to 20 s.
- the liner: 2.4 s (shorter than A's 4 s fade), no fades, ``sn_rem`` 1.400: song B starts at
  the liner's EOM, 1.0 s in (17 s), while A is still fading and the liner's tail plays on to
  18.4 s. All three decks sound from 17 to 18.4 s.
- song B: 20 s.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

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

from backend.domain.liners import PlayableLiner  # noqa: E402
from backend.domain.streaming import CuePoints, ItemRef  # noqa: E402
from backend.playout.assets import ensure_stream_assets  # noqa: E402
from backend.playout.liquidsoap_process import (  # noqa: E402
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    long_path,
    start_session,
)
from backend.services.streaming.payload import liner_payload  # noqa: E402
from backend.services.streaming.sessions import Assigned, LinerSlot  # noqa: E402
from tests.playout.test_session_liq_decks import job  # noqa: E402, F401 - the fixture
from tests.playout.test_session_liq_errors import (  # noqa: E402
    FIRST_AUDIO_TIMEOUT_S,
    FREQUENCIES,
    SessionSetup,
    engine_lines,
    rs_trace,
)
from tests.services.streaming.schedule import DAY, STATION, song  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

SONG_S = 20.0
SONG_FADE_S = 4.0
LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
LINER_CUES = CuePoints(0, 2_400, 0, 0, 1_400, 0.0)
LINER_EOM_S = 1.0
PUSH_SLACK_S = 0.15
ENGINE_SLACK_S = 0.5
"""``test_session_liq_decks.OVERLAP_SLACK_S``: push latency plus timing noise."""
_ITEM_GET = re.compile(r"/items/(\d+)$")
_STARTED = re.compile(r" deck=(\S+) seq=(\d+) t=([0-9.]+)")

type Rewrite = Callable[[dict[str, Any]], dict[str, Any]]


def song_cues(item: dict[str, Any]) -> dict[str, Any]:
    """A song as the service sends it today: no fade-in (D112), a 4 s fade-out and EOM."""
    item["annotations"].update({"liq_fade_in": "0.000", "liq_fade_out": "4.000", "sn_rem": "4.000"})
    return item


def liner_item(path: Path) -> Rewrite:
    """The liner exactly as the stream service sends it for seq 1 (``liner_payload``)."""
    slot = LinerSlot(
        liner=PlayableLiner(LINER_ID, f"{LINER_ID}.flac", LINER_CUES),
        path=path,
        after=Assigned(ItemRef(DAY, 0), song("06:00:00", int(SONG_S)), 0),
        after_span_ms=int(SONG_S * 1000),
    )
    payload = liner_payload(1, slot)

    def rewrite(item: dict[str, Any]) -> dict[str, Any]:
        return {"path": payload.path, "annotations": dict(payload.annotations)}

    return rewrite


def _proxy(upstream_port: int, rewrites: Mapping[int, Rewrite]) -> type[BaseHTTPRequestHandler]:
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
            if status == 200 and route and int(route.group(1)) in rewrites:
                body = json.dumps(rewrites[int(route.group(1))](json.loads(body))).encode()
            self._reply(status, body)

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(*self._forward("POST"))

    return Handler


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


@dataclass(frozen=True)
class LinerSession:
    process: subprocess.Popen[bytes]
    stub: StubLog
    started_at: float
    port: int
    engine_log: Path


@contextmanager
def liner_session(
    engine_job: object,
    stub: StubSession,
    setup: SessionSetup,
    rewrites: Mapping[int, Rewrite],
) -> Iterator[LinerSession]:
    """Liquidsoap -> rewriting proxy -> stub. On exit all three are stopped."""
    stub_server, stub_log = start_stub(stub)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _proxy(stub_server.server_port, rewrites))
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
        process = start_session(engine_job.assign, os.environ, endpoint, engine)  # type: ignore[attr-defined]
        yield LinerSession(process, stub_log, started_at, endpoint.harbor_port, endpoint.log_path)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        _stop(proxy)
        _stop(stub_server)


def engine_starts(engine_log: Path) -> dict[int, tuple[str, float]]:
    """Each seq's first ``RS STARTED`` line: its deck and its time on the engine's clock."""
    starts: dict[int, tuple[str, float]] = {}
    for line in engine_lines(engine_log, "STARTED"):
        found = _STARTED.search(line)
        if found:
            starts.setdefault(int(found.group(2)), (found.group(1), float(found.group(3))))
    return starts


def test_short_liner_overlaps_three_decks_without_a_gap(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,  # noqa: F811
) -> None:
    liner_file = make_tone(
        "ffmpeg",
        tmp_path / "liners" / str(STATION) / f"{LINER_ID}.flac",
        ToneSpec(FREQUENCIES[1], LINER_CUES.cue_out_ms / 1000),
    )
    songs = [
        make_tone("ffmpeg", tmp_path / f"t{seq}.flac", ToneSpec(FREQUENCIES[seq], SONG_S))
        for seq in (0, 2)
    ]
    items = (
        StubItem(path=long_path(songs[0]), span_s=SONG_S, title="Song A"),
        StubItem(path=long_path(liner_file), span_s=2.4, title="unused: the proxy's liner"),
        StubItem(path=long_path(songs[1]), span_s=SONG_S, title="Song B"),
    )
    stub = StubSession(session_id="t1", token=secrets.token_hex(8), items=items)
    rewrites = {0: song_cues, 1: liner_item(liner_file), 2: song_cues}
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    with liner_session(job, stub, setup, rewrites) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        # The schedule ends after song B and the stream closes (end of schedule).
        record_for(response, 120, tmp_path / "out.mp3")
        response.close()
        failed, started = session.stub.failed.seqs(), session.stub.started.seqs()
        starts = engine_starts(session.engine_log)
        play_failed = engine_lines(session.engine_log, "PLAY_FAILED")
        waits = engine_lines(session.engine_log, "DECK_WAIT")
        trace = rs_trace(session.engine_log)

    assert failed == [] and play_failed == [], f"items reported failed: {failed}\n{trace}"
    assert started == [0, 1, 2], f"started {started}\n{trace}"
    assert waits == [], f"a push waited for a free deck\n{trace}"
    (a_deck, a_at), (liner_deck, liner_at), (b_deck, b_at) = starts[0], starts[1], starts[2]
    assert len({a_deck, liner_deck, b_deck}) == 3, f"not three decks\n{trace}"
    eom_at = a_at + SONG_S - SONG_FADE_S
    assert eom_at - PUSH_SLACK_S <= liner_at <= eom_at + 0.5, f"liner not at A's EOM\n{trace}"
    assert abs(b_at - liner_at - LINER_EOM_S) <= ENGINE_SLACK_S, (
        f"B not at the liner's EOM\n{trace}"
    )
    assert b_at < a_at + SONG_S, f"A had stopped before B started\n{trace}"
