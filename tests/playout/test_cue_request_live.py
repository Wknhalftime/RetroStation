"""A reported song gets real cues, end to end on the cue consumer (slow; skipped without
LIQUIDSOAP_PATH or ffmpeg; needs PostgreSQL).

Spec: D79 ("The owner (E1's pipeline) analyses that audio ... storing cues or a failed row as
E1 does"); Cue pre-computation (autocue trims silence). A generated tone with 2 s of lead
silence; the assertion is about the trimmed silence, not an absolute loudness.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row

import backend.tasks.stream_cue_tasks as tasks_module
from backend.tasks.stream_cue_tasks import FailureMemory
from tests.integration import stream_seed as seed
from tests.playout.test_cue_analysis_live import tone

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.timeout(300)]


def test_a_reported_song_gets_real_cues(
    tmp_path: Path, liquidsoap_exe: Path, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    song = tone(tmp_path / "song.flac", amplitude=0.25, lead_s=2.0, tone_s=10.0, tail_s=3.0)
    stat = song.stat()
    audio = seed.audio_hash()
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        file_id = seed.library_file(
            conn,
            duration_ms=15_000,
            file_size=stat.st_size,
            file_mtime_ns=stat.st_mtime_ns,
            audio_hash=audio,
        )
        conn.execute("UPDATE library_files SET file_path = %s WHERE id = %s", (str(song), file_id))
        conn.commit()
    configured = SimpleNamespace(
        database_url=migrated_db,
        liquidsoap_path=liquidsoap_exe,
        stream_work_dir=tmp_path / "stream",
    )
    monkeypatch.setattr(tasks_module, "get_settings", lambda: configured)
    monkeypatch.setattr(tasks_module, "FAILURES", FailureMemory())
    tasks_module.stream_cue_request_task.call_local(str(file_id))
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        row = conn.execute("SELECT * FROM stream_cues WHERE audio_hash = %s", (audio,)).fetchone()
    assert row is not None
    assert row["analysis_failed"] is False
    assert 1_500 <= row["cue_in_ms"] <= 2_500  # the 2 s of lead silence, trimmed
