"""Extract the EOM ending-rule test fixture from Lance's labelled picks.

Each pick from the labeling page (one JSON object per line: ``audio_hash``, ``eom_ms``,
``ending``, ...) becomes one fixture entry. The entry holds the song's stored autocue cues and
its loudness tail, so the acceptance tests in ``tests/domain/test_ending_acceptance.py`` run
anywhere, without the library's audio.

    uv run python scripts/extract_ending_fixtures.py <labels.jsonl> <out.json>

Re-run after the labels change or the loudness definition changes. The decode here is the
fixture's own, and follows the same definition ``backend.playout.loudness.read_tail`` must meet
(pinned by ``tests/playout/test_loudness.py``):

- decode to mono at 16000 Hz, so a 50 ms frame is exactly 800 samples;
- 50 ms frames of mean-square energy;
- each frame in dB, relative to the mean energy of the frames louder than -60 dBFS;
- the tail runs from 60 s before the stored Stop (``cue_out_ms``), or the file's start, to the
  end of the file.

It reads the database named by ``DATABASE_URL`` (default: the dev database) and FFmpeg from
``FFMPEG_PATH`` (default: ``ffmpeg`` on PATH). It writes nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

SAMPLE_RATE = 16000  # 50 ms is exactly 800 samples
FRAME_MS = 50
TAIL_MS = 60_000
FLOOR_DB = -60.0
DEFAULT_DATABASE_URL = "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation"

_CUES_SQL = """
SELECT c.cue_in_ms, c.cue_out_ms, c.fade_in_ms, c.fade_out_ms, c.start_next_ms, c.gain_db,
       (SELECT min(f.file_path) FROM library_files f WHERE f.audio_hash = c.audio_hash
          AND f.file_status = 'present') AS path
FROM stream_cues c
WHERE c.audio_hash = %s
"""


def decode_frames_db(ffmpeg: str, path: str) -> np.ndarray:
    """Every 50 ms frame of ``path`` in dB, relative to the song's mean energy."""
    pcm = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            path,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    samples = np.frombuffer(pcm, dtype=np.float32).astype(np.float64)
    frame = SAMPLE_RATE * FRAME_MS // 1000
    count = -(-len(samples) // frame)
    padded = np.zeros(count * frame)
    padded[: len(samples)] = samples
    energy = np.maximum((padded.reshape(count, frame) ** 2).mean(axis=1), 1e-12)
    db = 10 * np.log10(energy)
    loud = energy[db > FLOOR_DB]
    reference = 10 * np.log10(loud.mean()) if len(loud) else 0.0
    return np.asarray(db - reference)


def entry(pick: dict[str, Any], row: dict[str, Any], frames_db: np.ndarray) -> dict[str, Any]:
    """One fixture entry: the pick, its stored cues and its loudness tail."""
    start_frame = max(0, (row["cue_out_ms"] - TAIL_MS) // FRAME_MS)
    return {
        "audio_hash": pick["audio_hash"],
        "title": pick["title"],
        "artist": pick["artist"],
        "station": pick["station"],
        "tag": pick["ending"] or "untagged",
        "moved": pick["moved"],
        "lance_eom_ms": pick["eom_ms"],
        "cues": {
            key: row[key]
            for key in (
                "cue_in_ms",
                "cue_out_ms",
                "fade_in_ms",
                "fade_out_ms",
                "start_next_ms",
                "gain_db",
            )
        },
        "tail": {
            "start_ms": start_frame * FRAME_MS,
            "frame_ms": FRAME_MS,
            "level_db": [round(float(v), 1) for v in frames_db[start_frame:]],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    ffmpeg = os.environ.get("FFMPEG_PATH", "ffmpeg")
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    picks = [
        json.loads(line)
        for line in args.labels.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    entries = []
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as conn:
        for pick in picks:
            row = conn.execute(_CUES_SQL, (pick["audio_hash"],)).fetchone()
            if row is None or row["path"] is None:
                print(f"skipped, no cues or file: {pick['audio_hash']}", file=sys.stderr)
                continue
            entries.append(entry(pick, row, decode_frames_db(ffmpeg, row["path"])))
    entries.sort(key=lambda e: e["audio_hash"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    body = ",\n".join(json.dumps(e, separators=(",", ":")) for e in entries)
    args.out.write_text(f"[\n{body}\n]\n", encoding="utf-8")  # one song per line
    print(f"wrote {len(entries)} entries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
