"""Engine benchmark for tune-in streaming (spec: Delivery, PR A). Windows only.

One stage per subcommand; each writes its own JSON (--out) and prints it:
  latency    3 sequential + 5 concurrent sessions: seconds from launch to first MP3 byte
  resources  N sessions with readers: CPU % of one core and RSS per Liquidsoap process
  soak       one session for M minutes over mixed-length tones with 3 broken paths
  gate       merge stage JSON files and print the verdict (exit 1 on any failure)

Every stage first warms Liquidsoap's script cache (about 5 s cold), as the app will at
startup, so launches measure the session and not the type-checker. The latency stage
reports that build time as ``cache_build_s``; the gate does not judge it.

Needs LIQUIDSOAP_PATH and ffmpeg (FFMPEG_PATH, default "ffmpeg").
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil
from stream_gate import (
    SILENCE_FILTER,
    GateLimits,
    gate_failures,
    merge_reports,
    silences_s,
    soak_problems,
)
from stream_stub import (
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.playout.assets import ensure_stream_assets  # noqa: E402
from backend.playout.liquidsoap_process import (  # noqa: E402
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    long_path,
    start_session,
    warm_script_cache,
)
from backend.playout.windows_job import KillOnCloseJob  # noqa: E402

SOAK_SPANS_S = [10, 12, 15, 20, 30, 40]
SOAK_BROKEN = {7, 19, 31}
START_NEXT_S = 4  # sn_rem in stream_stub.item_annotations: decks overlap by this much
type Report = dict[str, object]


@dataclass(frozen=True)
class BenchConfig:
    ffmpeg: str
    work: Path
    engine: EngineConfig
    cache_build_s: float


@dataclass(frozen=True)
class StartedSession:
    process: subprocess.Popen[bytes]
    launched_at: float
    latency_s: float
    response: http.client.HTTPResponse
    log: StubLog


@dataclass(frozen=True)
class Draining:
    session: StartedSession
    stop: threading.Event
    reader: threading.Thread


def build_items(spans: list[int], broken: set[int], config: BenchConfig) -> list[StubItem]:
    items: list[StubItem] = []
    for seq, span in enumerate(spans):
        if seq in broken:
            items.append(StubItem(path=r"\\?\D:\no\such\file.flac", span_s=span, title="Gone"))
            continue
        tone = ToneSpec(frequency=300 + 50 * (seq % 24), span_s=span)
        path = make_tone(config.ffmpeg, config.work / "tones" / f"t{seq % 24}_{span}.flac", tone)
        items.append(StubItem(path=long_path(path), span_s=span, title=f"Tone {seq}"))
    return items


def start(job: KillOnCloseJob, items: list[StubItem], config: BenchConfig) -> StartedSession:
    """Serve ``items`` from a stub, launch one session and wait for its first MP3 byte."""
    session_id = f"b{secrets.token_hex(3)}"
    stub = StubSession(session_id=session_id, token=secrets.token_hex(8), items=tuple(items))
    server, log = start_stub(stub)
    endpoint = SessionEndpoint(
        session_id=session_id,
        harbor_port=free_port(),
        backend_url=stub_base_url(server, session_id),
        session_token=stub.token,
        log_path=config.work / "logs" / f"{session_id}.log",
    )
    launched_at = time.monotonic()
    process = start_session(job.assign, os.environ, endpoint, config.engine)
    latency, response = first_audio(endpoint.harbor_port, launched_at, timeout_s=15)
    return StartedSession(process, launched_at, latency, response, log)


def drain(session: StartedSession) -> Draining:
    """Read and discard the stream in the background, as a listening player would."""
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set() and session.response.read1(8192):
            pass
        session.response.close()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    return Draining(session, stop, reader)


def stop(draining: Draining) -> None:
    draining.stop.set()
    draining.reader.join(timeout=5)
    draining.session.process.wait(timeout=10)


def run_latency(config: BenchConfig) -> Report:
    items = build_items([30] * 4, set(), config)
    sequential: list[float] = []
    with KillOnCloseJob() as job:
        for _ in range(3):
            session = start(job, items, config)
            sequential.append(session.latency_s)
            stop(drain(session))
        concurrent = [drain(start(job, items, config)) for _ in range(5)]
        for draining in concurrent:
            stop(draining)
    concurrent_s = [d.session.latency_s for d in concurrent]
    return {
        "latency_sequential_s": sequential,
        "latency_concurrent_s": concurrent_s,
        "latency_s": sequential + concurrent_s,
        "cache_build_s": config.cache_build_s,
    }


@dataclass(frozen=True)
class ResourceRun:
    sessions: int
    seconds: int
    warm_up_s: int = 10


def run_resources(run: ResourceRun, config: BenchConfig) -> Report:
    items = build_items([40] * 10, set(), config)
    with KillOnCloseJob() as job:
        running = [drain(start(job, items, config)) for _ in range(run.sessions)]
        procs = [psutil.Process(d.session.process.pid) for d in running]
        time.sleep(run.warm_up_s)
        for proc in procs:
            proc.cpu_percent(interval=None)
        cpu: list[list[float]] = [[] for _ in procs]
        rss = [0.0 for _ in procs]
        private = [0.0 for _ in procs]
        for _ in range(run.seconds):
            time.sleep(1)
            for i, proc in enumerate(procs):
                cpu[i].append(proc.cpu_percent(interval=None))
                memory = proc.memory_info()
                rss[i] = max(rss[i], memory.rss / 1_048_576)
                private[i] = max(private[i], getattr(memory, "private", 0) / 1_048_576)
        for draining in running:
            stop(draining)
    return {
        "cpu_percent": [sum(c) / len(c) for c in cpu],
        "rss_mb": rss,
        "private_mb": private,
        "resource_sessions": run.sessions,
        "resource_seconds": run.seconds,
    }


def soak_spans(minutes: float) -> list[int]:
    """Mixed spans whose overlapped playing time exceeds the run by 2 minutes."""
    spans: list[int] = []
    while sum(span - START_NEXT_S for span in spans) < minutes * 60 + 120:
        spans.append(SOAK_SPANS_S[len(spans) % len(SOAK_SPANS_S)])
    return spans


@dataclass(frozen=True)
class SoakRecording:
    mp3: Path
    log: StubLog
    launched_at: float
    planned_s: float
    streamed_s: float
    ended_at: float


def record_soak(minutes: float, config: BenchConfig) -> SoakRecording:
    items = build_items(soak_spans(minutes), SOAK_BROKEN, config)
    mp3 = config.work / "soak.mp3"
    mp3.unlink(missing_ok=True)
    with KillOnCloseJob() as job:
        session = start(job, items, config)
        streamed = record_for(session.response, minutes * 60, mp3)
        ended_at = time.monotonic()
        session.response.close()
        session.process.wait(timeout=10)
    return SoakRecording(mp3, session.log, session.launched_at, minutes * 60, streamed, ended_at)


def analyse_soak(recording: SoakRecording, config: BenchConfig) -> Report:
    detect = subprocess.run(
        [
            config.ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(recording.mp3),
            "-af",
            SILENCE_FILTER,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    started = recording.log.started
    return {
        "soak_problems": soak_problems(started.seqs(), recording.log.failed.seqs(), SOAK_BROKEN),
        "silences_s": silences_s(detect.stderr),
        "start_times_s": [at - recording.launched_at for at in started.times()],
        "soak_end_s": recording.ended_at - recording.launched_at,
        "items_started": len(started.seqs()),
        "failed_seqs": recording.log.failed.seqs(),
        "decode_exit_code": detect.returncode,
        "soak_planned_s": recording.planned_s,
        "soak_streamed_s": recording.streamed_s,
        "max_span_s": max(SOAK_SPANS_S),
    }


def bench_config(args: argparse.Namespace) -> BenchConfig:
    exe = os.environ.get("LIQUIDSOAP_PATH")
    if not exe:
        raise SystemExit("LIQUIDSOAP_PATH (env): set it to liquidsoap.exe")
    ffmpeg = os.environ.get("FFMPEG_PATH", "ffmpeg")
    work: Path = args.work
    for folder in ("logs", "cache", "tones"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    assets = ensure_stream_assets(ffmpeg, work / "assets")
    engine = EngineConfig(
        exe=Path(exe),
        script=SESSION_SCRIPT,
        cache_dir=work / "cache",
        filler=assets.filler,
        intro_sfx=assets.static_intro,
    )
    began = time.monotonic()
    warm_script_cache(os.environ, engine)
    return BenchConfig(
        ffmpeg=ffmpeg, work=work, engine=engine, cache_build_s=time.monotonic() - began
    )


def emit(report: Report, out: Path | None) -> None:
    text = json.dumps(report, indent=2)
    print(text)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def cmd_latency(args: argparse.Namespace) -> None:
    emit(run_latency(bench_config(args)), args.out)


def cmd_resources(args: argparse.Namespace) -> None:
    emit(run_resources(ResourceRun(args.sessions, args.seconds), bench_config(args)), args.out)


def cmd_soak(args: argparse.Namespace) -> None:
    config = bench_config(args)
    emit(analyse_soak(record_soak(args.minutes, config), config), args.out)


def cmd_gate(args: argparse.Namespace) -> None:
    reports: list[Report] = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports]
    merged = merge_reports(reports)
    limits = GateLimits()
    failures = gate_failures(merged, limits)
    emit(merged | {"gate_limits": asdict(limits), "gate_failures": failures}, args.out)
    if failures:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    stages: dict[str, Callable[[argparse.Namespace], None]] = {
        "latency": cmd_latency,
        "resources": cmd_resources,
        "soak": cmd_soak,
    }
    for name, handler in stages.items():
        stage = sub.add_parser(name)
        stage.add_argument("--work", type=Path, default=Path("var/bench-stream"))
        stage.add_argument("--out", type=Path)
        stage.set_defaults(handler=handler)
    sub.choices["resources"].add_argument("--sessions", type=int, default=5)
    sub.choices["resources"].add_argument("--seconds", type=int, default=60)
    sub.choices["soak"].add_argument("--minutes", type=float, default=10.0)
    gate = sub.add_parser("gate")
    gate.add_argument("reports", nargs="+")
    gate.add_argument("--out", type=Path)
    gate.set_defaults(handler=cmd_gate)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
