"""Stand-in for RetroStation's internal stream API, for tests and scripts/bench_stream.py.

It implements the Backend <-> Liquidsoap contract in
docs/superpowers/specs/2026-09-27-tune-in-streaming-design.md:
GET  /internal/stream/sessions/{id}/items/{seq}  -> 200 item | 410 end | other = retry later
POST /internal/stream/sessions/{id}/items/{seq}/started | /failed
"""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@dataclass(frozen=True)
class StubItem:
    path: str
    span_s: float
    title: str
    gain_db: float = 0.0


@dataclass(frozen=True)
class StubSession:
    """``hold_from``: items from this seq on answer 503 until ``StubLog.released`` is set."""

    session_id: str
    token: str
    items: tuple[StubItem, ...]
    hold_from: int | None = None


@dataclass(frozen=True)
class SeqLog:
    """Sequence numbers in arrival order, stamped with time.monotonic()."""

    entries: list[tuple[float, int]] = field(default_factory=list)
    changed: threading.Condition = field(default_factory=threading.Condition)

    def record(self, seq: int) -> None:
        with self.changed:
            self.entries.append((time.monotonic(), seq))
            self.changed.notify_all()

    def seqs(self) -> list[int]:
        with self.changed:
            return [seq for _, seq in self.entries]

    def times(self) -> list[float]:
        with self.changed:
            return [at for at, _ in self.entries]

    def wait_for(self, seq: int, timeout_s: float) -> float:
        """When ``seq`` was first recorded; waits up to ``timeout_s`` for it."""
        deadline = time.monotonic() + timeout_s
        with self.changed:
            while True:
                for at, recorded in self.entries:
                    if recorded == seq:
                        return at
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"seq {seq} not recorded within {timeout_s} s")
                self.changed.wait(remaining)


@dataclass(frozen=True)
class StubLog:
    """What Liquidsoap asked for and reported; ``released`` ends a ``hold_from`` outage."""

    requested: SeqLog = field(default_factory=SeqLog)
    started: SeqLog = field(default_factory=SeqLog)
    failed: SeqLog = field(default_factory=SeqLog)
    released: threading.Event = field(default_factory=threading.Event)


def item_annotations(seq: int, item: StubItem) -> dict[str, str]:
    return {
        "item_seq": str(seq),
        "liq_cue_in": "0.",
        "liq_cue_out": f"{item.span_s:.3f}",
        "liq_fade_in": "3.",
        "liq_fade_out": "4.",
        "sn_rem": "4.",
        "liq_amplify": f"{item.gain_db:.1f} dB",
        "title": item.title,
        "artist": "Stub",
    }


def _held(session: StubSession, log: StubLog, seq: int) -> bool:
    return session.hold_from is not None and seq >= session.hold_from and not log.released.is_set()


def _handler(session: StubSession, log: StubLog) -> type[BaseHTTPRequestHandler]:
    prefix = ["", "internal", "stream", "sessions", session.session_id, "items"]
    reports = {"started": log.started, "failed": log.failed}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _route(self) -> tuple[int, str] | None:
            parts = self.path.split("/")
            if parts[:6] != prefix or len(parts) < 7 or not parts[6].isdigit():
                return None
            return int(parts[6]), "/".join(parts[7:])

        def _reply(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            route = self._route()
            if route is None or route[1] != "":
                return self._reply(404)
            if self.headers.get("X-Session-Token") != session.token:
                return self._reply(403)
            seq = route[0]
            log.requested.record(seq)
            if _held(session, log, seq):
                return self._reply(503)
            if seq >= len(session.items):
                return self._reply(410)
            item = session.items[seq]
            body = {"path": item.path, "annotations": item_annotations(seq, item)}
            return self._reply(200, json.dumps(body).encode())

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            route = self._route()
            if route is None or route[1] not in reports:
                return self._reply(404)
            if self.headers.get("X-Session-Token") != session.token:
                return self._reply(403)
            reports[route[1]].record(route[0])
            return self._reply(204)

    return Handler


def start_stub(session: StubSession) -> tuple[ThreadingHTTPServer, StubLog]:
    """Serve ``session`` on a free loopback port in a daemon thread."""
    log = StubLog()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(session, log))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, log


def stub_base_url(server: ThreadingHTTPServer, session_id: str) -> str:
    return f"http://127.0.0.1:{server.server_port}/internal/stream/sessions/{session_id}"


@dataclass(frozen=True)
class ToneSpec:
    frequency: int
    span_s: float


def make_tone(ffmpeg: str, out: Path, tone: ToneSpec) -> Path:
    """A stereo sine at -12 dBFS peak; distinct frequencies make each item identifiable."""
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        # aevalsrc, not sine: ffmpeg's sine source is fixed at 1/8 (-18 dBFS). One
        # expression per channel: upmixing mono with -ac 2 would cost another 3 dB.
        wave = f"0.25*sin({tone.frequency}*2*PI*t)"
        source = f"aevalsrc={wave}|{wave}:c=stereo:s=44100:d={tone.span_s}"
        staged = out.with_suffix(".partial.flac")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-c:a",
                "flac",
                str(staged),
            ],
            check=True,
            timeout=60,
        )
        staged.replace(out)
    return out


def free_port() -> int:
    """A currently free loopback port (tests run in parallel under xdist)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def first_audio(
    port: int, since: float, timeout_s: float
) -> tuple[float, http.client.HTTPResponse]:
    """Retry GET /stream until audio arrives; seconds since ``since`` and the open response."""
    deadline = since + timeout_s
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/stream")
            response = connection.getresponse()
        except OSError:
            connection.close()
            time.sleep(0.02)
            continue
        if response.status == 200 and response.read(1):
            return time.monotonic() - since, response
        connection.close()
        time.sleep(0.02)
    raise TimeoutError(f"no audio on port {port} within {timeout_s} s")


def record_for(
    response: http.client.HTTPResponse,
    seconds: float,
    out: Path,
    stop: threading.Event | None = None,
) -> float:
    """Append the stream to ``out`` for ``seconds``, until EOF or until ``stop`` is set.

    Returns the seconds spent reading, so a stream that ended early reads short.
    """
    began = time.monotonic()
    deadline = began + seconds
    with out.open("ab") as sink:
        while time.monotonic() < deadline and not (stop is not None and stop.is_set()):
            chunk = response.read1(16384)
            if not chunk:
                break
            sink.write(chunk)
    return time.monotonic() - began
