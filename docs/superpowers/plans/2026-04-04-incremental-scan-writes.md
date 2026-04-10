# Incremental Scan Writes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the library scan write files to the database incrementally during the scan instead of all-at-once at the end, with chunked commits for resilience.

**Architecture:** Replace the current "collect-in-memory then bulk-write" pattern with a callback-driven approach. `scan_directory` will accept an `on_file`/`on_quarantine` callback pair that the task layer uses to write each result immediately. The DB connection commits in configurable-size chunks (default 100). The unused SELECT-after-INSERT in `upsert` gets a write-only sibling method. The core scan logic is extracted into a testable plain function separate from the Huey decorator.

**Tech Stack:** Python 3.12, psycopg3, pytest, Huey task queue

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `backend/repositories/library_files.py` | Modify (line 10) | Add `upsert_write_only` abstract method |
| `backend/db/repositories/library_files.py` | Modify (after line 121) | Add `upsert_write_only` — INSERT only, no SELECT |
| `tests/fakes/library_files.py` | Modify (after line 18) | Add `upsert_write_only` to fake — delegate to `upsert` |
| `backend/repositories/library_quarantine.py` | Modify (line 8) | Add `create_write_only` abstract method |
| `backend/db/repositories/library_quarantine.py` | Modify (after line 36) | Add `create_write_only` — INSERT only, no SELECT |
| `tests/fakes/library_quarantine.py` | Modify (after line 11) | Add `create_write_only` to fake — delegate to `create` |
| `backend/services/library_scan_service.py` | Modify (lines 308-360) | Add `on_file` and `on_quarantine` callbacks to `scan_directory` |
| `backend/tasks/library_tasks.py` | Modify (lines 24-131) | Extract `_run_scan` function; open DB before scan, write incrementally, commit in chunks |
| `tests/services/test_library_scan.py` | Modify | Add tests for new callback behavior |
| `tests/tasks/__init__.py` | Create (empty) | Package init |
| `tests/tasks/test_library_tasks.py` | Create | Tests for chunked commit behavior using extracted `_run_scan` |

---

### Task 1: Add Write-Only Upsert to Library Files Repository

**Files:**
- Modify: `backend/repositories/library_files.py:10`
- Modify: `backend/db/repositories/library_files.py:51-121`
- Modify: `tests/fakes/library_files.py:12-18`

- [ ] **Step 1: Add abstract method to interface**

In `backend/repositories/library_files.py`, add after line 10 (after the existing `upsert` abstract method):

```python
@abstractmethod
def upsert_write_only(self, file: LibraryFile) -> None: ...
```

- [ ] **Step 2: Implement `upsert_write_only` in PgLibraryFileRepository**

In `backend/db/repositories/library_files.py`, add a new method after the existing `upsert` (after line 121):

```python
def upsert_write_only(self, file: LibraryFile) -> None:
    """INSERT or UPDATE a library file without reading back the row."""
    self._conn.execute(
        """
        INSERT INTO library_files (
            id, file_path, file_hash, format, enrichment_status,
            trace_id, recording_id, recording_mbid, artist_mbid,
            album_artist_mbid, release_mbid, release_title, release_type,
            release_type_secondary, release_status, track_title,
            track_number, disc_number, duration_ms, bitrate, raw_metadata,
            indexed_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s,
            NOW()
        )
        ON CONFLICT (file_path) DO UPDATE SET
            file_hash              = EXCLUDED.file_hash,
            format                 = EXCLUDED.format,
            enrichment_status      = EXCLUDED.enrichment_status,
            trace_id               = EXCLUDED.trace_id,
            recording_id           = EXCLUDED.recording_id,
            recording_mbid         = EXCLUDED.recording_mbid,
            artist_mbid            = EXCLUDED.artist_mbid,
            album_artist_mbid      = EXCLUDED.album_artist_mbid,
            release_mbid           = EXCLUDED.release_mbid,
            release_title          = EXCLUDED.release_title,
            release_type           = EXCLUDED.release_type,
            release_type_secondary = EXCLUDED.release_type_secondary,
            release_status         = EXCLUDED.release_status,
            track_title            = EXCLUDED.track_title,
            track_number           = EXCLUDED.track_number,
            disc_number            = EXCLUDED.disc_number,
            duration_ms            = EXCLUDED.duration_ms,
            bitrate                = EXCLUDED.bitrate,
            raw_metadata           = EXCLUDED.raw_metadata,
            indexed_at             = NOW()
        """,
        (
            file.id,
            file.file_path,
            file.file_hash,
            file.format,
            file.enrichment_status.value,
            file.trace_id,
            file.recording_id,
            file.recording_mbid,
            file.artist_mbid,
            file.album_artist_mbid,
            file.release_mbid,
            file.release_title,
            file.release_type.value if file.release_type else None,
            file.release_type_secondary,
            file.release_status.value if file.release_status else None,
            file.track_title,
            file.track_number,
            file.disc_number,
            file.duration_ms,
            file.bitrate,
            json.dumps(file.raw_metadata) if file.raw_metadata is not None else None,
        ),
    )
```

- [ ] **Step 3: Add `upsert_write_only` to the fake**

In `tests/fakes/library_files.py`, add after the existing `upsert` method (after line 18):

```python
def upsert_write_only(self, file: LibraryFile) -> None:
    self.upsert(file)
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `pytest tests/test_fakes_implement_abcs.py tests/services/test_library_scan.py -v`
Expected: All tests PASS — the new abstract method is implemented in both the Pg class and the fake.

- [ ] **Step 5: Commit**

```bash
git add backend/repositories/library_files.py backend/db/repositories/library_files.py tests/fakes/library_files.py
git commit -m "feat: add upsert_write_only to library files repository (no SELECT round-trip)"
```

---

### Task 2: Add Write-Only Create to Library Quarantine Repository

**Files:**
- Modify: `backend/repositories/library_quarantine.py:8`
- Modify: `backend/db/repositories/library_quarantine.py:24-36`
- Modify: `tests/fakes/library_quarantine.py:9-11`

- [ ] **Step 1: Add abstract method to interface**

In `backend/repositories/library_quarantine.py`, add after line 8 (after the existing `create` abstract method):

```python
@abstractmethod
def create_write_only(self, entry: LibraryQuarantine) -> None: ...
```

- [ ] **Step 2: Implement `create_write_only` in PgLibraryQuarantineRepository**

In `backend/db/repositories/library_quarantine.py`, add after the existing `create` method (after line 36):

```python
def create_write_only(self, entry: LibraryQuarantine) -> None:
    """Insert a quarantine entry without reading back the row."""
    self._conn.execute(
        """INSERT INTO library_quarantine (id, file_path, error_message, trace_id)
           VALUES (%s, %s, %s, %s)""",
        (entry.id, entry.file_path, entry.error_message, entry.trace_id),
    )
```

- [ ] **Step 3: Add `create_write_only` to the fake**

In `tests/fakes/library_quarantine.py`, add after the existing `create` method (after line 11):

```python
def create_write_only(self, entry: LibraryQuarantine) -> None:
    self.create(entry)
```

- [ ] **Step 4: Run ABC compliance tests**

Run: `pytest tests/test_fakes_implement_abcs.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/repositories/library_quarantine.py backend/db/repositories/library_quarantine.py tests/fakes/library_quarantine.py
git commit -m "feat: add create_write_only to quarantine repository (no SELECT round-trip)"
```

---

### Task 3: Add Callbacks to `scan_directory`

**Files:**
- Modify: `backend/services/library_scan_service.py:308-360`
- Test: `tests/services/test_library_scan.py`

- [ ] **Step 1: Write a failing test for on_file callback**

Add the import at the top of `tests/services/test_library_scan.py` if not already present:

```python
from backend.domain.models import LibraryFile, LibraryQuarantine
```

Add to class `TestScanDirectory`:

```python
def test_on_file_callback_called_per_extracted_file(self) -> None:
    if not AUDIO_DIR.exists():
        pytest.skip("Audio fixtures directory not found")
    received: list[LibraryFile] = []
    files, _ = scan_directory(AUDIO_DIR, on_file=lambda lf: received.append(lf))
    assert len(received) == len(files)
    assert {lf.file_path for lf in received} == {lf.file_path for lf in files}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_library_scan.py::TestScanDirectory::test_on_file_callback_called_per_extracted_file -v`
Expected: FAIL — `scan_directory` does not accept `on_file` parameter.

- [ ] **Step 3: Write a failing test for on_quarantine callback**

Add to class `TestScanDirectory`:

```python
def test_on_quarantine_callback_called_per_quarantined_file(self) -> None:
    if not AUDIO_DIR.exists():
        pytest.skip("Audio fixtures directory not found")
    received: list[LibraryQuarantine] = []
    _, quarantine = scan_directory(
        AUDIO_DIR, on_quarantine=lambda q: received.append(q)
    )
    assert len(received) == len(quarantine)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/services/test_library_scan.py::TestScanDirectory::test_on_quarantine_callback_called_per_quarantined_file -v`
Expected: FAIL — `scan_directory` does not accept `on_quarantine` parameter.

- [ ] **Step 5: Implement the callbacks in `scan_directory`**

Modify the signature and body in `backend/services/library_scan_service.py`. Replace lines 308-360 with:

```python
def scan_directory(
    root: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_file: Callable[[LibraryFile], None] | None = None,
    on_quarantine: Callable[[LibraryQuarantine], None] | None = None,
) -> tuple[list[LibraryFile], list[LibraryQuarantine]]:
    """
    Walk *root* recursively and extract tags from all supported audio files.

    Returns ``(files, quarantine)`` where *quarantine* contains an entry for
    every file that raised a :exc:`mutagen.MutagenError`.

    Optional callbacks:
      *on_file* — called with each successfully extracted :class:`LibraryFile`.
      *on_quarantine* — called with each :class:`LibraryQuarantine` entry.
      *on_progress* — called with ``(processed, total, current_path)`` every
        50 files and on the final file.
    """
    candidates = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    total = len(candidates)

    files: list[LibraryFile] = []
    quarantine: list[LibraryQuarantine] = []

    for processed_idx, path in enumerate(candidates, start=1):
        try:
            lf = extract_tags(path)
            files.append(lf)
            if on_file is not None:
                on_file(lf)
        except MutagenError as exc:
            logger.warning("Quarantining %s: %s", path, exc)
            entry = LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=str(exc),
            )
            quarantine.append(entry)
            if on_quarantine is not None:
                on_quarantine(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error scanning %s: %s", path, exc)
            entry = LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=f"{type(exc).__name__}: {exc}",
            )
            quarantine.append(entry)
            if on_quarantine is not None:
                on_quarantine(entry)

        if on_progress is not None and (
            processed_idx % 50 == 0 or processed_idx == total
        ):
            on_progress(processed_idx, total, str(path))

    return files, quarantine
```

- [ ] **Step 6: Run both new tests and all existing tests**

Run: `pytest tests/services/test_library_scan.py -v`
Expected: All tests PASS including the two new callback tests.

- [ ] **Step 7: Commit**

```bash
git add backend/services/library_scan_service.py tests/services/test_library_scan.py
git commit -m "feat: add on_file and on_quarantine callbacks to scan_directory"
```

---

### Task 4: Refactor `library_scan_task` for Incremental Writes with Chunked Commits

This is the core change. The task layer opens the library DB connection **before** calling `scan_directory` and writes each file/quarantine entry via the callbacks, committing every `COMMIT_CHUNK_SIZE` writes. The core logic is extracted into a plain `_run_scan()` function so it can be tested without Huey.

**Files:**
- Modify: `backend/tasks/library_tasks.py:1-131`
- Create: `tests/tasks/__init__.py` (empty)
- Create: `tests/tasks/test_library_tasks.py`

- [ ] **Step 1: Write failing tests for chunked commit behavior**

Create `tests/tasks/__init__.py` (empty file).

Create `tests/tasks/test_library_tasks.py`:

```python
"""Tests for the incremental-write and chunked-commit behavior of _run_scan."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.domain.enums import EnrichmentStatus
from backend.domain.models import LibraryFile, LibraryQuarantine


def _make_lf(idx: int) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/tmp/test_{idx}.mp3",
        file_hash="a" * 64,
        format="mp3",
        enrichment_status=EnrichmentStatus.PENDING,
    )


def _make_q(idx: int) -> LibraryQuarantine:
    return LibraryQuarantine(
        id=uuid4(),
        file_path=f"/tmp/bad_{idx}.mp3",
        error_message="bad file",
    )


class TestRunScanChunkedCommits:
    """Test that _run_scan commits at COMMIT_CHUNK_SIZE boundaries."""

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_commit_fires_at_chunk_boundary(self, mock_scan: MagicMock) -> None:
        """With chunk_size=3 and 5 files, commit should fire at file 3 and at end."""
        from backend.tasks.library_tasks import _run_scan

        files = [_make_lf(i) for i in range(5)]

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            for lf in files:
                on_file(lf)
            return files, []

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=3,
        )

        # commit at file 3 (chunk boundary) + commit for remaining 2 at end = 2 commits
        assert mock_conn.commit.call_count == 2

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_commit_fires_once_when_exact_chunk(self, mock_scan: MagicMock) -> None:
        """With chunk_size=3 and exactly 3 files, commit at boundary + no trailing = 1 commit."""
        from backend.tasks.library_tasks import _run_scan

        files = [_make_lf(i) for i in range(3)]

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            for lf in files:
                on_file(lf)
            return files, []

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=3,
        )

        # Exactly at boundary: commit fires at 3, pending_writes resets to 0,
        # trailing commit is skipped because pending_writes == 0
        assert mock_conn.commit.call_count == 1

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_upsert_write_only_called_for_each_file(self, mock_scan: MagicMock) -> None:
        """Each file should trigger upsert_write_only, not the old upsert."""
        from backend.tasks.library_tasks import _run_scan

        files = [_make_lf(i) for i in range(3)]

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            for lf in files:
                on_file(lf)
            return files, []

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=100,
        )

        assert mock_repos.library_files.upsert_write_only.call_count == 3
        # The old upsert should NOT be called
        mock_repos.library_files.upsert.assert_not_called()

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_create_write_only_called_for_quarantine(self, mock_scan: MagicMock) -> None:
        """Quarantine entries should use create_write_only."""
        from backend.tasks.library_tasks import _run_scan

        quarantine = [_make_q(i) for i in range(2)]

        def fake_scan(root: Path, **kwargs):
            on_quarantine = kwargs["on_quarantine"]
            for q in quarantine:
                on_quarantine(q)
            return [], quarantine

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=100,
        )

        assert mock_repos.library_quarantine.create_write_only.call_count == 2
        mock_repos.library_quarantine.create.assert_not_called()

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_mixed_files_and_quarantine_share_chunk_counter(
        self, mock_scan: MagicMock
    ) -> None:
        """Files and quarantine entries both count toward the chunk boundary."""
        from backend.tasks.library_tasks import _run_scan

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            on_quarantine = kwargs["on_quarantine"]
            # 2 files + 1 quarantine = 3 writes = chunk boundary at chunk_size=3
            on_file(_make_lf(0))
            on_file(_make_lf(1))
            on_quarantine(_make_q(0))
            return [_make_lf(0), _make_lf(1)], [_make_q(0)]

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=3,
        )

        # Exactly at boundary, so 1 commit (no trailing)
        assert mock_conn.commit.call_count == 1

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_returns_counts(self, mock_scan: MagicMock) -> None:
        """_run_scan should return (files_written, quarantine_written)."""
        from backend.tasks.library_tasks import _run_scan

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            on_quarantine = kwargs["on_quarantine"]
            on_file(_make_lf(0))
            on_file(_make_lf(1))
            on_quarantine(_make_q(0))
            return [_make_lf(0), _make_lf(1)], [_make_q(0)]

        mock_scan.side_effect = fake_scan

        result = _run_scan(
            root_path="/tmp/music",
            library_conn=MagicMock(),
            repos=MagicMock(),
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=100,
        )

        assert result == (2, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/tasks/test_library_tasks.py -v`
Expected: FAIL — `_run_scan` does not exist yet.

- [ ] **Step 3: Rewrite `library_scan_task` — extract `_run_scan` and implement incremental writes**

Replace the **entire contents** of `backend/tasks/library_tasks.py` with:

```python
from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.progress_tracking import PgTaskProgressRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import LibraryFile, LibraryQuarantine, TaskProgress
from backend.services.library_scan_service import scan_directory
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

COMMIT_CHUNK_SIZE = 100


def _run_scan(
    *,
    root_path: str,
    library_conn: psycopg.Connection,
    repos: RepositoryFactory,
    progress_repo: PgTaskProgressRepository,
    task_id: str,
    chunk_size: int = COMMIT_CHUNK_SIZE,
) -> tuple[int, int]:
    """Core scan logic — extracted from the Huey task so it is directly testable.

    Opens no connections itself; callers provide them.
    Returns ``(files_written, quarantine_written)``.
    """
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    files_written = 0
    quarantine_written = 0
    pending_writes = 0

    # --- Callbacks ---

    def on_file(lf: LibraryFile) -> None:
        nonlocal pending_writes, files_written
        repos.library_files.upsert_write_only(lf)
        files_written += 1
        pending_writes += 1
        if pending_writes >= chunk_size:
            library_conn.commit()
            nonlocal pending_writes
            pending_writes = 0

    def on_quarantine(entry: LibraryQuarantine) -> None:
        nonlocal pending_writes, quarantine_written
        repos.library_quarantine.create_write_only(entry)
        quarantine_written += 1
        pending_writes += 1
        if pending_writes >= chunk_size:
            library_conn.commit()
            nonlocal pending_writes
            pending_writes = 0

    def on_progress(processed: int, total: int, current_path: str) -> None:
        nonlocal last_progress
        last_progress = {
            "processed": processed,
            "total": total,
            "current_path": current_path,
        }
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
            )
        )

    # --- Run scan with callbacks ---
    scan_directory(
        Path(root_path),
        on_progress=on_progress,
        on_file=on_file,
        on_quarantine=on_quarantine,
    )

    # Commit any remaining writes from the last partial chunk
    if pending_writes > 0:
        library_conn.commit()

    return files_written, quarantine_written


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_task(root_path: str) -> str:
    """Scan a directory for audio files and persist results to the DB."""
    logger.info("library_scan_task_started", root=root_path)
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    progress_conn = None
    progress_repo: PgTaskProgressRepository | None = None
    library_conn = None

    try:
        # Autocommit connection for progress tracking
        progress_conn = psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        )
        progress_repo = PgTaskProgressRepository(progress_conn)

        # Initial progress record
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        # Open library connection BEFORE scan so callbacks can write immediately
        library_conn = psycopg.connect(
            settings.database_url, autocommit=False, row_factory=dict_row
        )
        repos = RepositoryFactory(library_conn)

        files_written, quarantine_written = _run_scan(
            root_path=root_path,
            library_conn=library_conn,
            repos=repos,
            progress_repo=progress_repo,
            task_id=task_id,
        )

        # Mark completed AFTER library data commit succeeds
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.COMPLETED,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )

        logger.info(
            "library_scan_task_complete",
            root=root_path,
            files_indexed=files_written,
            quarantined=quarantine_written,
        )

    except Exception as exc:
        if library_conn is not None:
            with contextlib.suppress(Exception):
                library_conn.rollback()

        if progress_conn is not None and progress_repo is not None:
            with contextlib.suppress(Exception):
                progress_repo.upsert(
                    TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.SCAN,
                        status=TaskStatus.FAILED,
                        progress_data={**last_progress, "error": str(exc)},
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()

    return root_path
```

**Important note about `nonlocal` in nested closures:** Python requires all `nonlocal` declarations at the top of the function. The `on_file` and `on_quarantine` closures each use `nonlocal pending_writes` — the declaration must appear once, before any assignment. The code above has a subtle error: `nonlocal pending_writes` appears twice in each closure (once at the top, once inline). The correct form for each closure is:

```python
def on_file(lf: LibraryFile) -> None:
    nonlocal pending_writes, files_written
    repos.library_files.upsert_write_only(lf)
    files_written += 1
    pending_writes += 1
    if pending_writes >= chunk_size:
        library_conn.commit()
        pending_writes = 0
```

(The `nonlocal` at the top already covers `pending_writes` — no second declaration needed. The `pending_writes = 0` inside the `if` works because of the declaration at the top.)

- [ ] **Step 4: Run new tests to verify they pass**

Run: `pytest tests/tasks/test_library_tasks.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/tasks/library_tasks.py tests/tasks/__init__.py tests/tasks/test_library_tasks.py
git commit -m "feat: incremental DB writes during scan with chunked commits (100-file batches)"
```

---

### Task 5: Verify End-to-End and Clean Up

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 2: Verify no lint errors in modified files**

Run: `ruff check backend/tasks/library_tasks.py backend/services/library_scan_service.py backend/db/repositories/library_files.py backend/db/repositories/library_quarantine.py backend/repositories/library_files.py backend/repositories/library_quarantine.py tests/fakes/library_files.py tests/fakes/library_quarantine.py tests/tasks/test_library_tasks.py`
Expected: No errors.

- [ ] **Step 3: Verify backward compatibility of scan_directory**

Search for any other callers of `scan_directory` besides `library_tasks.py`:

Run: `grep -rn "scan_directory" backend/ tests/`

Confirm the signature is backward-compatible — all new params are optional, return value unchanged.

- [ ] **Step 4: Final commit if any cleanup was needed**

```bash
git add -A
git commit -m "chore: clean up imports and lint after incremental scan refactor"
```
