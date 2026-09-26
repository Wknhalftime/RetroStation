"""Integration: library_hash_backfill_task against a real database."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.library import LibraryFile
from backend.tasks.library_hash_backfill_tasks import library_hash_backfill_task

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_db_url(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point get_settings() at the migrated test DB for this test."""
    from backend.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    try:
        yield migrated_db
    finally:
        get_settings.cache_clear()


def _seed(db_url: str, tmp_path: Path, n: int) -> list[Path]:
    paths = []
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for i in range(n):
            path = tmp_path / f"{i:02d}.flac"
            path.write_bytes(bytes([i]) * 64)
            st = path.stat()
            repo.upsert(LibraryFile(
                id=uuid4(), file_path=str(path), file_hash=None, format="flac",
                file_size=st.st_size, file_mtime_ns=st.st_mtime_ns,
            ))
            paths.append(path)
        conn.commit()
    return paths


def test_task_hashes_backlog_and_completes_progress(
    settings_db_url: str, tmp_path: Path,
) -> None:
    paths = _seed(settings_db_url, tmp_path, 3)

    library_hash_backfill_task.call_local()

    with psycopg.connect(settings_db_url, row_factory=dict_row) as conn:
        hashes = {
            r["file_path"]: r["file_hash"]
            for r in conn.execute("SELECT file_path, file_hash FROM library_files").fetchall()
        }
        runs = conn.execute(
            "SELECT status, progress_data FROM progress_tracking WHERE task_type = %s",
            ("hash_backfill",),
        ).fetchall()
    for path in paths:
        assert hashes[str(path)] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert [r["status"] for r in runs] == ["completed"]
    assert runs[0]["progress_data"]["hashed"] == 3


def test_task_failure_is_reported_and_reraised(
    settings_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(settings_db_url, tmp_path, 1)

    class BoomError(RuntimeError):
        pass

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise BoomError("deliberate")

    monkeypatch.setattr(
        "backend.tasks.library_hash_backfill_tasks.backfill_hash_batch", _explode,
    )
    with pytest.raises(BoomError):
        library_hash_backfill_task.call_local()

    with psycopg.connect(settings_db_url, row_factory=dict_row) as conn:
        statuses = [
            r["status"] for r in conn.execute(
                "SELECT status FROM progress_tracking WHERE task_type = %s",
                ("hash_backfill",),
            ).fetchall()
        ]
    assert statuses == ["failed"]
