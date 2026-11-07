"""The EOM labeling page: Lance places EOM on one song at a time, to teach the ending rules.

A dev tool, not part of the app (EOM ending rules, docs/superpowers/specs/2026-11-07-eom-
ending-rules-design.md). It reads the dev database, serves the song files straight from disk, and
appends each EOM placed to ``labels.jsonl`` next to this file, one JSON object per line. Nothing
goes on air: it never writes the database or the cue edits.

    uv run python scripts/eom_teach/server.py
    then open http://localhost:8077/

``labels.jsonl`` holds Lance's first 51 picks (2026-11-07, WZOR, KSTZ, KKDM). Stage 1's
acceptance fixture was made from them with ``scripts/extract_ending_fixtures.py``. Stage 2 adds
picks tagged ``chorus`` (a repeating-chorus fade) and ``live`` (music ending into applause).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import psycopg

HERE = Path(__file__).resolve().parent
LABELS = HERE / "labels.jsonl"
PAGE = HERE / "page.html"
PORT = 8077
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation"
)
STATIONS = {"WZOR": "Rock", "KSTZ": "Hot AC", "KKDM": "CHR / Top 40"}
QUEUE_SIZE = 300

_MEDIA = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/mp4",
    "ogg": "audio/ogg",
}

# The most-played analysed songs on a station that have no on-air cue edit yet, one row per
# audio. Autocue's EOM is where the next song starts: cue_out - start_next.
_QUEUE_SQL = """
with plays as (
    select r.file_id, count(*) as plays
    from play_file_resolution r
    join stations s on s.id = r.station_id
    where s.call_letters = %(call)s and r.file_id is not null
    group by r.file_id
), songs as (
    select distinct on (f.audio_hash)
        f.id, f.track_title, f.artist_name, f.audio_hash, f.duration_ms, f.format,
        c.cue_in_ms, c.cue_out_ms, c.start_next_ms, c.fade_out_ms, p.plays
    from plays p
    join library_files f on f.id = p.file_id
    join stream_cues c on c.audio_hash = f.audio_hash
    where f.missing_since is null
      and not c.analysis_failed and not c.audio_silent and c.orphaned_at is null
      and c.cue_out_ms is not null
      and not exists (select 1 from stream_cue_edits e where e.audio_hash = f.audio_hash)
    order by f.audio_hash, p.plays desc
)
select * from songs order by plays desc limit %(limit)s
"""


def _labelled() -> set[str]:
    """Audio hashes already labelled in this spike, so they are not served again."""
    if not LABELS.exists():
        return set()
    done: set[str] = set()
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            done.add(json.loads(line)["audio_hash"])
    return done


_ROWS: dict[str, list[tuple[Any, ...]]] = {}
"""Each station's song list, read once per server run (the query takes about ten seconds)."""


def _queue(call: str) -> list[dict[str, Any]]:
    done = _labelled()
    if call not in _ROWS:
        with psycopg.connect(DATABASE_URL) as conn:
            _ROWS[call] = conn.execute(_QUEUE_SQL, {"call": call, "limit": QUEUE_SIZE}).fetchall()
    rows = _ROWS[call]
    songs = []
    for r in rows:
        if r[3] in done:
            continue
        songs.append(
            {
                "file_id": str(r[0]),
                "title": r[1],
                "artist": r[2],
                "audio_hash": r[3],
                "duration_ms": r[4],
                "format": r[5],
                "cue_in_ms": r[6],
                "cue_out_ms": r[7],
                "autocue_eom_ms": r[7] - r[8],
                "fade_out_ms": r[9],
                "plays": r[10],
            }
        )
    return songs


def _file_of(file_id: str) -> tuple[Path, str] | None:
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "select file_path, format from library_files where id = %s", (file_id,)
        ).fetchone()
    if row is None:
        return None
    return Path(row[0]), _MEDIA.get(str(row[1]).lower(), "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write("%s\n" % (format % args))

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/stations":
            self._json(STATIONS)
        elif url.path == "/api/queue":
            call = parse_qs(url.query).get("station", ["WZOR"])[0]
            if call not in STATIONS:
                self._json({"error": "unknown station"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(_queue(call))
        elif url.path == "/api/count":
            self._json({"labelled": len(_labelled())})
        elif url.path.startswith("/api/audio/"):
            found = _file_of(url.path.removeprefix("/api/audio/"))
            if found is None or not found[0].exists():
                self._json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send(HTTPStatus.OK, found[0].read_bytes(), found[1])
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/label":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        label = json.loads(self.rfile.read(length).decode("utf-8"))
        label["saved_at"] = datetime.now(UTC).isoformat()
        with LABELS.open("a", encoding="utf-8") as out:
            out.write(json.dumps(label) + "\n")
        self._json({"ok": True, "labelled": len(_labelled())})


class _Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy bind the same port and silently take some
    # of the requests; refuse instead, so an old copy still running is noticed.
    allow_reuse_address = False


def main() -> None:
    server = _Server(("127.0.0.1", PORT), Handler)
    print(f"EOM labeling page on http://localhost:{PORT}/  (labels -> {LABELS})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
