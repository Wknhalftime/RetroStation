# Auto-Enrichment Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically detect new/modified/missing audio files via periodic polling with folder-hash diffing, then chain into the enrichment pipeline.

**Architecture:** A Huey periodic task polls every 4 minutes, computes mtime+size-based folder hashes, diffs against stored hashes to find changed folders, then enqueues a smart targeted scan that only processes changed files. Scan completion chains into enrichment automatically.

**Tech Stack:** Python 3.13+, psycopg, Huey (SqliteHuey, -w 1, crontab), PostgreSQL advisory locks, structlog.

**Spec reference:** `docs/superpowers/specs/2026-04-05-auto-enrichment-watcher-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `backend/db/migrations/0010_watcher_layer.sql` | DDL: `library_folders`, `library_folder_staged_hashes`, `file_status` column |
| `backend/domain/enums.py` | `FileStatus` enum |
| `backend/domain/models.py` | `LibraryFolder` dataclass, `file_status` field on `LibraryFile` |
| `backend/repositories/library_folders.py` | Abstract `LibraryFolderRepository` |
| `backend/db/repositories/library_folders.py` | PG implementation |
| `backend/db/repositories/library_files.py` | Upsert conflict clause change, new query methods |
| `backend/repositories/library_files.py` | Abstract: new method signatures |
| `backend/services/repository_factory.py` | Register folder repo |
| `backend/services/folder_hash_service.py` | Walk tree, compute hashes, diff, coalesce |
| `backend/services/library_scan_service.py` | `scan_folder_smart()` — per-folder diffing |
| `backend/tasks/library_watcher_tasks.py` | Periodic poll task + `library_scan_files_task` |
| `backend/tasks/library_tasks.py` | Add enrichment chaining to existing `library_scan_task` |
| `backend/tasks/huey_app.py` | Import new task module |
| `tests/fakes/library_files.py` | Update fake for new methods |
| `tests/fakes/library_folders.py` | New fake |
| `tests/services/test_folder_hash_service.py` | Unit tests |
| `tests/services/test_smart_scan.py` | Unit tests for all 6 scan scenarios |
| `tests/integration/test_pg_library_folders.py` | Integration tests |
| `tests/integration/test_watcher_poll.py` | Integration tests for full poll cycle |
| `tests/integration/test_upsert_enrichment_preservation.py` | Integration test for upsert behavior |

---

### Task 1: Database Migration

**Files:**
- Create: `backend/db/migrations/0010_watcher_layer.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- Watcher layer: folder hash tree, staged hashes, file status tracking.

CREATE TABLE library_folders (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id   UUID        REFERENCES library_folders(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    full_path   TEXT        NOT NULL UNIQUE,
    folder_hash TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_library_folders_parent ON library_folders(parent_id);

CREATE TABLE library_folder_staged_hashes (
    folder_id       UUID        NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
    new_hash        TEXT        NOT NULL,
    staged_by_task  TEXT        NOT NULL,
    staged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (folder_id, staged_by_task)
);

ALTER TABLE library_files
    ADD COLUMN file_status TEXT NOT NULL DEFAULT 'PRESENT';

CREATE INDEX idx_library_files_file_status
    ON library_files(file_status)
    WHERE file_status != 'PRESENT';
```

- [ ] **Step 2: Verify migration applies cleanly**

Run: `python -c "import psycopg; from backend.db.migrations import run_migrations; conn = psycopg.connect('postgresql://retrostation:retrostation-dev@localhost:5432/retrostation_test'); run_migrations(conn); conn.commit(); print('OK')"`

Expected: `OK` — migration `0010_watcher_layer` applied without error.

- [ ] **Step 3: Update test conftest TRUNCATE list**

Add `library_folders` and `library_folder_staged_hashes` to the TRUNCATE statement in `tests/conftest.py:38-48`:

```python
        conn.execute("""
            TRUNCATE log_events, log_identities, log_artists,
                     playlists, broadcast_days, stations,
                     matches, global_mapping_rules,
                     artists, works, recordings,
                     library_files, library_quarantine,
                     song_masters, format_overrides,
                     mb_cache, progress_tracking, user_settings,
                     system_logs, library_folder_staged_hashes,
                     library_folders
            CASCADE
        """)
```

Note: `library_folder_staged_hashes` must appear before `library_folders` due to FK dependency (CASCADE handles it, but ordering is clearer).

- [ ] **Step 4: Commit**

```bash
git add backend/db/migrations/0010_watcher_layer.sql tests/conftest.py
git commit -m "feat: add watcher layer migration (library_folders, staged_hashes, file_status)"
```

---

### Task 2: FileStatus Enum and Model Changes

**Files:**
- Modify: `backend/domain/enums.py:48-54`
- Modify: `backend/domain/models.py:117-141`

- [ ] **Step 1: Write the failing test for FileStatus enum**

Create `tests/domain/test_enums.py` (or add to existing):

```python
"""Tests for new FileStatus enum."""
from backend.domain.enums import FileStatus


class TestFileStatus:
    def test_present_value(self) -> None:
        assert FileStatus.PRESENT == "PRESENT"

    def test_missing_value(self) -> None:
        assert FileStatus.MISSING == "MISSING"

    def test_deleted_value(self) -> None:
        assert FileStatus.DELETED == "DELETED"

    def test_is_str_enum(self) -> None:
        assert isinstance(FileStatus.PRESENT, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_enums.py -v`

Expected: `ImportError: cannot import name 'FileStatus'`

- [ ] **Step 3: Add FileStatus enum to enums.py**

Add after `EnrichmentStatus` (line 54 of `backend/domain/enums.py`):

```python
class FileStatus(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    DELETED = "DELETED"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/domain/test_enums.py -v`

Expected: All 4 tests PASS.

- [ ] **Step 5: Write the failing test for LibraryFile.file_status**

Add to `tests/domain/test_enums.py`:

```python
from backend.domain.models import LibraryFile
from uuid import uuid4


class TestLibraryFileStatus:
    def test_default_file_status_is_present(self) -> None:
        lf = LibraryFile(
            id=uuid4(),
            file_path="/test.flac",
            file_hash="abc123",
            format="flac",
        )
        assert lf.file_status == FileStatus.PRESENT

    def test_file_status_can_be_set(self) -> None:
        lf = LibraryFile(
            id=uuid4(),
            file_path="/test.flac",
            file_hash="abc123",
            format="flac",
            file_status=FileStatus.MISSING,
        )
        assert lf.file_status == FileStatus.MISSING
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_enums.py::TestLibraryFileStatus -v`

Expected: `TypeError: __init__() got an unexpected keyword argument 'file_status'`

- [ ] **Step 7: Add file_status field to LibraryFile dataclass**

In `backend/domain/models.py`, add the import of `FileStatus` to the import block (line 8-19) and add the field after `enrichment_status` (line 123):

Add to imports:
```python
from backend.domain.enums import (
    EnrichmentStatus,
    FileStatus,
    MatchStatus,
    ...
)
```

Add field after `enrichment_status` line:
```python
    file_status: FileStatus = FileStatus.PRESENT
```

- [ ] **Step 8: Write the failing test for LibraryFolder model**

Add to `tests/domain/test_enums.py`:

```python
from backend.domain.models import LibraryFolder


class TestLibraryFolderModel:
    def test_create_folder(self) -> None:
        folder = LibraryFolder(
            id=uuid4(),
            name="jazz",
            full_path="/music/jazz",
        )
        assert folder.name == "jazz"
        assert folder.full_path == "/music/jazz"
        assert folder.parent_id is None
        assert folder.folder_hash is None
```

- [ ] **Step 9: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_enums.py::TestLibraryFolderModel -v`

Expected: `ImportError: cannot import name 'LibraryFolder'`

- [ ] **Step 10: Add LibraryFolder dataclass to models.py**

Add after the `LibraryQuarantine` class in `backend/domain/models.py`:

```python
@dataclass
class LibraryFolder:
    id: UUID
    name: str
    full_path: str
    parent_id: UUID | None = None
    folder_hash: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

- [ ] **Step 11: Run all tests to verify nothing broke**

Run: `uv run pytest tests/domain/test_enums.py -v`

Expected: All tests PASS.

- [ ] **Step 12: Commit**

```bash
git add backend/domain/enums.py backend/domain/models.py tests/domain/test_enums.py
git commit -m "feat: add FileStatus enum, LibraryFolder model, file_status field on LibraryFile"
```

---

### Task 3: Library Folders Repository

**Files:**
- Create: `backend/repositories/library_folders.py`
- Create: `backend/db/repositories/library_folders.py`
- Create: `tests/fakes/library_folders.py`
- Create: `tests/integration/test_pg_library_folders.py`
- Modify: `backend/services/repository_factory.py:27-48`

- [ ] **Step 1: Write the abstract repository**

Create `backend/repositories/library_folders.py`:

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import LibraryFolder


class LibraryFolderRepository(ABC):
    @abstractmethod
    def upsert(self, folder: LibraryFolder) -> None: ...

    @abstractmethod
    def get_by_path(self, full_path: str) -> LibraryFolder | None: ...

    @abstractmethod
    def get_children(self, parent_id: UUID) -> list[LibraryFolder]: ...

    @abstractmethod
    def get_all(self) -> list[LibraryFolder]: ...

    @abstractmethod
    def update_hash(self, folder_id: UUID, folder_hash: str) -> None: ...

    @abstractmethod
    def stage_hashes(
        self, hashes: list[tuple[UUID, str]], task_id: str
    ) -> None: ...

    @abstractmethod
    def commit_staged_hashes(self, task_id: str) -> int: ...

    @abstractmethod
    def clear_staged_hashes(self, task_id: str) -> None: ...

    @abstractmethod
    def has_any(self) -> bool: ...
```

- [ ] **Step 2: Write the fake repository**

Create `tests/fakes/library_folders.py`:

```python
from __future__ import annotations

from uuid import UUID

from backend.domain.models import LibraryFolder
from backend.repositories.library_folders import LibraryFolderRepository


class FakeLibraryFolderRepository(LibraryFolderRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LibraryFolder] = {}
        self._staged: dict[str, list[tuple[UUID, str]]] = {}

    def upsert(self, folder: LibraryFolder) -> None:
        existing = self.get_by_path(folder.full_path)
        if existing:
            self._data[existing.id] = folder
        else:
            self._data[folder.id] = folder

    def get_by_path(self, full_path: str) -> LibraryFolder | None:
        return next(
            (f for f in self._data.values() if f.full_path == full_path), None
        )

    def get_children(self, parent_id: UUID) -> list[LibraryFolder]:
        return [f for f in self._data.values() if f.parent_id == parent_id]

    def get_all(self) -> list[LibraryFolder]:
        return list(self._data.values())

    def update_hash(self, folder_id: UUID, folder_hash: str) -> None:
        if folder_id in self._data:
            self._data[folder_id].folder_hash = folder_hash

    def stage_hashes(
        self, hashes: list[tuple[UUID, str]], task_id: str
    ) -> None:
        self._staged[task_id] = hashes

    def commit_staged_hashes(self, task_id: str) -> int:
        staged = self._staged.pop(task_id, [])
        for folder_id, new_hash in staged:
            self.update_hash(folder_id, new_hash)
        return len(staged)

    def clear_staged_hashes(self, task_id: str) -> None:
        self._staged.pop(task_id, None)

    def has_any(self) -> bool:
        return len(self._data) > 0
```

- [ ] **Step 3: Write integration tests for PG repository**

Create `tests/integration/test_pg_library_folders.py`:

```python
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_folders import PgLibraryFolderRepository
from backend.domain.models import LibraryFolder


def test_upsert_and_get_by_path(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        folder = LibraryFolder(
            id=uuid4(),
            name="jazz",
            full_path="/music/jazz",
            folder_hash="abc123",
        )
        repo.upsert(folder)
        conn.commit()

        result = repo.get_by_path("/music/jazz")
        assert result is not None
        assert result.name == "jazz"
        assert result.folder_hash == "abc123"


def test_upsert_updates_existing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        folder = LibraryFolder(
            id=uuid4(),
            name="jazz",
            full_path="/music/jazz2",
            folder_hash="hash1",
        )
        repo.upsert(folder)
        conn.commit()

        folder.folder_hash = "hash2"
        repo.upsert(folder)
        conn.commit()

        result = repo.get_by_path("/music/jazz2")
        assert result is not None
        assert result.folder_hash == "hash2"


def test_get_children(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        parent = LibraryFolder(
            id=uuid4(), name="music", full_path="/music"
        )
        child1 = LibraryFolder(
            id=uuid4(),
            name="jazz",
            full_path="/music/jazz",
            parent_id=parent.id,
        )
        child2 = LibraryFolder(
            id=uuid4(),
            name="rock",
            full_path="/music/rock",
            parent_id=parent.id,
        )
        repo.upsert(parent)
        repo.upsert(child1)
        repo.upsert(child2)
        conn.commit()

        children = repo.get_children(parent.id)
        assert len(children) == 2
        names = {c.name for c in children}
        assert names == {"jazz", "rock"}


def test_update_hash(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        folder = LibraryFolder(
            id=uuid4(), name="test", full_path="/test"
        )
        repo.upsert(folder)
        conn.commit()

        repo.update_hash(folder.id, "newhash")
        conn.commit()

        result = repo.get_by_path("/test")
        assert result is not None
        assert result.folder_hash == "newhash"


def test_stage_and_commit_hashes(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        f1 = LibraryFolder(id=uuid4(), name="a", full_path="/a")
        f2 = LibraryFolder(id=uuid4(), name="b", full_path="/b")
        repo.upsert(f1)
        repo.upsert(f2)
        conn.commit()

        task_id = "task-001"
        repo.stage_hashes([(f1.id, "hash_a"), (f2.id, "hash_b")], task_id)
        conn.commit()

        count = repo.commit_staged_hashes(task_id)
        conn.commit()

        assert count == 2
        assert repo.get_by_path("/a").folder_hash == "hash_a"
        assert repo.get_by_path("/b").folder_hash == "hash_b"


def test_staged_hashes_isolated_by_task_id(migrated_db: str) -> None:
    """Task A commits only its own staged hashes, not Task B's."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        f1 = LibraryFolder(id=uuid4(), name="x", full_path="/x")
        f2 = LibraryFolder(id=uuid4(), name="y", full_path="/y")
        repo.upsert(f1)
        repo.upsert(f2)
        conn.commit()

        repo.stage_hashes([(f1.id, "h1_a")], "task-a")
        repo.stage_hashes([(f2.id, "h2_b")], "task-b")
        conn.commit()

        # Commit only task-a
        count = repo.commit_staged_hashes("task-a")
        conn.commit()

        assert count == 1
        assert repo.get_by_path("/x").folder_hash == "h1_a"
        assert repo.get_by_path("/y").folder_hash is None  # task-b not committed


def test_has_any(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        assert repo.has_any() is False

        folder = LibraryFolder(id=uuid4(), name="z", full_path="/z")
        repo.upsert(folder)
        conn.commit()

        assert repo.has_any() is True
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_pg_library_folders.py -v`

Expected: `ModuleNotFoundError: No module named 'backend.db.repositories.library_folders'`

- [ ] **Step 5: Write the PG implementation**

Create `backend/db/repositories/library_folders.py`:

```python
from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.models import LibraryFolder
from backend.repositories.library_folders import LibraryFolderRepository


class PgLibraryFolderRepository(LibraryFolderRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryFolder:
        return LibraryFolder(
            id=row["id"],
            parent_id=row.get("parent_id"),
            name=row["name"],
            full_path=row["full_path"],
            folder_hash=row.get("folder_hash"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert(self, folder: LibraryFolder) -> None:
        self._conn.execute(
            """
            INSERT INTO library_folders (id, parent_id, name, full_path, folder_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (full_path) DO UPDATE SET
                parent_id   = EXCLUDED.parent_id,
                name        = EXCLUDED.name,
                folder_hash = EXCLUDED.folder_hash,
                updated_at  = NOW()
            """,
            (
                folder.id,
                folder.parent_id,
                folder.name,
                folder.full_path,
                folder.folder_hash,
            ),
        )

    def get_by_path(self, full_path: str) -> LibraryFolder | None:
        row = self._conn.execute(
            "SELECT * FROM library_folders WHERE full_path = %s",
            (full_path,),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_children(self, parent_id: UUID) -> list[LibraryFolder]:
        rows = self._conn.execute(
            "SELECT * FROM library_folders WHERE parent_id = %s",
            (parent_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_all(self) -> list[LibraryFolder]:
        rows = self._conn.execute(
            "SELECT * FROM library_folders ORDER BY full_path"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_hash(self, folder_id: UUID, folder_hash: str) -> None:
        self._conn.execute(
            """UPDATE library_folders
               SET folder_hash = %s, updated_at = NOW()
               WHERE id = %s""",
            (folder_hash, folder_id),
        )

    def stage_hashes(
        self, hashes: list[tuple[UUID, str]], task_id: str
    ) -> None:
        for folder_id, new_hash in hashes:
            self._conn.execute(
                """
                INSERT INTO library_folder_staged_hashes
                    (folder_id, new_hash, staged_by_task)
                VALUES (%s, %s, %s)
                ON CONFLICT (folder_id, staged_by_task) DO UPDATE SET
                    new_hash  = EXCLUDED.new_hash,
                    staged_at = NOW()
                """,
                (folder_id, new_hash, task_id),
            )

    def commit_staged_hashes(self, task_id: str) -> int:
        result = self._conn.execute(
            """
            UPDATE library_folders f
            SET folder_hash = s.new_hash,
                updated_at  = NOW()
            FROM library_folder_staged_hashes s
            WHERE f.id = s.folder_id
              AND s.staged_by_task = %s
            """,
            (task_id,),
        )
        count = result.rowcount
        self.clear_staged_hashes(task_id)
        return count

    def clear_staged_hashes(self, task_id: str) -> None:
        self._conn.execute(
            "DELETE FROM library_folder_staged_hashes WHERE staged_by_task = %s",
            (task_id,),
        )

    def has_any(self) -> bool:
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM library_folders) AS has_rows"
        ).fetchone()
        return bool(row["has_rows"]) if row else False
```

- [ ] **Step 6: Register in RepositoryFactory**

Add to `backend/services/repository_factory.py`:

Import at top:
```python
from backend.db.repositories.library_folders import PgLibraryFolderRepository
```

Add in `__init__` (after `self.library_quarantine` line 46):
```python
        self.library_folders = PgLibraryFolderRepository(conn)
```

- [ ] **Step 7: Run integration tests**

Run: `uv run pytest tests/integration/test_pg_library_folders.py -v`

Expected: All 7 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/repositories/library_folders.py backend/db/repositories/library_folders.py tests/fakes/library_folders.py tests/integration/test_pg_library_folders.py backend/services/repository_factory.py
git commit -m "feat: add library_folders repository with staged hash support"
```

---

### Task 4: Upsert Behavior Change — Preserve Enrichment Status

**Files:**
- Modify: `backend/db/repositories/library_files.py:51-121`
- Modify: `backend/repositories/library_files.py`
- Modify: `tests/fakes/library_files.py`
- Create: `tests/integration/test_upsert_enrichment_preservation.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_upsert_enrichment_preservation.py`:

```python
"""Test that upsert preserves enrichment_status when file_hash is unchanged."""
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.models import LibraryFile


def _make_file(
    *,
    file_path: str = "/music/track.flac",
    file_hash: str = "abc123",
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING,
    file_status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash=file_hash,
        format="flac",
        enrichment_status=enrichment_status,
        file_status=file_status,
        track_title="Test Track",
    )


def test_upsert_same_hash_preserves_enrichment(migrated_db: str) -> None:
    """Re-upserting with same file_hash should keep existing enrichment_status."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        # Insert as PENDING, then manually mark ENRICHED
        lf = _make_file(file_path="/preserve/same_hash.flac", file_hash="hash_a")
        repo.upsert(lf)
        conn.execute(
            "UPDATE library_files SET enrichment_status = 'enriched' WHERE file_path = %s",
            (lf.file_path,),
        )
        conn.commit()

        # Re-upsert with same hash but PENDING status
        lf2 = _make_file(
            file_path="/preserve/same_hash.flac",
            file_hash="hash_a",
            enrichment_status=EnrichmentStatus.PENDING,
        )
        repo.upsert_write_only(lf2)
        conn.commit()

        result = repo.get_by_path("/preserve/same_hash.flac")
        assert result is not None
        assert result.enrichment_status == EnrichmentStatus.ENRICHED


def test_upsert_different_hash_resets_enrichment(migrated_db: str) -> None:
    """Re-upserting with different file_hash should reset enrichment_status."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        lf = _make_file(file_path="/preserve/diff_hash.flac", file_hash="old_hash")
        repo.upsert(lf)
        conn.execute(
            "UPDATE library_files SET enrichment_status = 'enriched' WHERE file_path = %s",
            (lf.file_path,),
        )
        conn.commit()

        # Re-upsert with new hash
        lf2 = _make_file(
            file_path="/preserve/diff_hash.flac",
            file_hash="new_hash",
            enrichment_status=EnrichmentStatus.PENDING,
        )
        repo.upsert_write_only(lf2)
        conn.commit()

        result = repo.get_by_path("/preserve/diff_hash.flac")
        assert result is not None
        assert result.enrichment_status == EnrichmentStatus.PENDING


def test_upsert_sets_file_status_present(migrated_db: str) -> None:
    """Upsert should always set file_status to PRESENT (file exists on disk)."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        lf = _make_file(file_path="/preserve/status.flac")
        repo.upsert(lf)
        # Manually mark MISSING
        conn.execute(
            "UPDATE library_files SET file_status = 'MISSING' WHERE file_path = %s",
            (lf.file_path,),
        )
        conn.commit()

        # Re-upsert
        lf2 = _make_file(file_path="/preserve/status.flac")
        repo.upsert_write_only(lf2)
        conn.commit()

        result = repo.get_by_path("/preserve/status.flac")
        assert result is not None
        assert result.file_status == FileStatus.PRESENT


def test_get_by_folder_path(migrated_db: str) -> None:
    """get_by_folder_path returns all files whose path starts with given dir."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        repo.upsert_write_only(
            _make_file(file_path="/music/jazz/track1.flac")
        )
        repo.upsert_write_only(
            _make_file(file_path="/music/jazz/track2.flac")
        )
        repo.upsert_write_only(
            _make_file(file_path="/music/rock/track1.flac")
        )
        conn.commit()

        results = repo.get_by_folder_path("/music/jazz")
        assert len(results) == 2
        paths = {r.file_path for r in results}
        assert paths == {"/music/jazz/track1.flac", "/music/jazz/track2.flac"}


def test_mark_missing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        lf = _make_file(file_path="/music/gone.flac")
        repo.upsert_write_only(lf)
        conn.commit()

        repo.mark_missing(lf.file_path)
        conn.commit()

        result = repo.get_by_path("/music/gone.flac")
        assert result is not None
        assert result.file_status == FileStatus.MISSING
        # enrichment_status should be preserved
        assert result.enrichment_status == EnrichmentStatus.PENDING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_upsert_enrichment_preservation.py -v`

Expected: Failures — `file_status` column doesn't exist yet in `_row_to_model`, and methods `get_by_folder_path`/`mark_missing` don't exist.

- [ ] **Step 3: Add new abstract methods to LibraryFileRepository**

Add to `backend/repositories/library_files.py`:

```python
    @abstractmethod
    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]: ...

    @abstractmethod
    def mark_missing(self, file_path: str) -> None: ...
```

- [ ] **Step 4: Update PgLibraryFileRepository**

In `backend/db/repositories/library_files.py`:

Add `FileStatus` to imports:
```python
from backend.domain.enums import EnrichmentStatus, FileStatus, ReleaseStatus, ReleaseType
```

Update `_row_to_model` — add after the `enrichment_status` line:
```python
            file_status=FileStatus(row.get("file_status", "PRESENT")),
```

Update both `upsert()` and `upsert_write_only()` — change the `ON CONFLICT` clause. Replace:
```python
                enrichment_status      = EXCLUDED.enrichment_status,
```
with:
```python
                enrichment_status      = CASE
                    WHEN library_files.file_hash = EXCLUDED.file_hash
                    THEN library_files.enrichment_status
                    ELSE EXCLUDED.enrichment_status
                END,
                file_status            = 'PRESENT',
```

Add new methods at the end of the class:

```python
    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]:
        """Return all files whose path starts with folder_path/."""
        prefix = folder_path.rstrip("/") + "/"
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE file_path LIKE %s
                 AND file_path NOT LIKE %s""",
            (prefix + "%", prefix + "%/%"),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_missing(self, file_path: str) -> None:
        self._conn.execute(
            """UPDATE library_files
               SET file_status = 'MISSING'
               WHERE file_path = %s""",
            (file_path,),
        )
```

Note: `get_by_folder_path` returns files directly in the folder (not in subfolders) by excluding paths with additional `/` after the prefix. This is important for the per-folder scan logic.

- [ ] **Step 5: Update FakeLibraryFileRepository**

In `tests/fakes/library_files.py`, add:

```python
    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]:
        prefix = folder_path.rstrip("/") + "/"
        return [
            f for f in self._data.values()
            if f.file_path.startswith(prefix)
            and "/" not in f.file_path[len(prefix):]
        ]

    def mark_missing(self, file_path: str) -> None:
        from backend.domain.enums import FileStatus
        for f in self._data.values():
            if f.file_path == file_path:
                f.file_status = FileStatus.MISSING
                break
```

Also update `upsert` to match the new hash-preservation behavior:

```python
    def upsert(self, file: LibraryFile) -> LibraryFile:
        from backend.domain.enums import FileStatus
        existing = self.get_by_path(file.file_path)
        if existing:
            # Preserve enrichment_status if hash unchanged
            if existing.file_hash == file.file_hash:
                file.enrichment_status = existing.enrichment_status
            file.file_status = FileStatus.PRESENT
            self._data[existing.id] = file
            return file
        self._data[file.id] = file
        return file
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/integration/test_upsert_enrichment_preservation.py -v`

Expected: All 5 tests PASS.

- [ ] **Step 7: Run existing tests to verify no regressions**

Run: `uv run pytest tests/integration/test_pg_library_repos.py tests/services/test_library_scan.py -v`

Expected: All PASS. The existing upsert test `test_library_file_upsert_and_get` re-upserts with a different hash (`"newhashabc"`), so it should still overwrite enrichment_status.

- [ ] **Step 8: Commit**

```bash
git add backend/repositories/library_files.py backend/db/repositories/library_files.py tests/fakes/library_files.py tests/integration/test_upsert_enrichment_preservation.py
git commit -m "feat: upsert preserves enrichment_status when file_hash unchanged, add file_status support"
```

---

### Task 5: Folder Hash Service

**Files:**
- Create: `backend/services/folder_hash_service.py`
- Create: `tests/services/test_folder_hash_service.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_folder_hash_service.py`:

```python
"""Unit tests for folder_hash_service."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from backend.domain.models import LibraryFolder
from backend.services.folder_hash_service import (
    canonicalize_path,
    coalesce_paths,
    compute_folder_hash,
    diff_tree,
)
from tests.fakes.library_folders import FakeLibraryFolderRepository


class TestComputeFolderHash:
    def test_empty_folder(self, tmp_path: Path) -> None:
        result = compute_folder_hash(tmp_path)
        # Empty folder = hash of empty string
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected

    def test_hash_changes_when_file_added(self, tmp_path: Path) -> None:
        h1 = compute_folder_hash(tmp_path)
        (tmp_path / "track.flac").write_bytes(b"\x00" * 100)
        h2 = compute_folder_hash(tmp_path)
        assert h1 != h2

    def test_hash_stable_for_same_content(self, tmp_path: Path) -> None:
        (tmp_path / "track.flac").write_bytes(b"\x00" * 100)
        h1 = compute_folder_hash(tmp_path)
        h2 = compute_folder_hash(tmp_path)
        assert h1 == h2

    def test_hash_uses_mtime_and_size(self, tmp_path: Path) -> None:
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 100)
        h1 = compute_folder_hash(tmp_path)

        # Change content (different size) -> different hash
        f.write_bytes(b"\x00" * 200)
        h2 = compute_folder_hash(tmp_path)
        assert h1 != h2

    def test_ignores_non_audio_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("hello")
        (tmp_path / "track.flac").write_bytes(b"\x00" * 100)

        h1 = compute_folder_hash(tmp_path)

        # Adding another non-audio file should not change hash
        (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8" * 50)
        h2 = compute_folder_hash(tmp_path)
        assert h1 == h2

    def test_includes_subfolder_hashes(self, tmp_path: Path) -> None:
        sub = tmp_path / "subfolder"
        sub.mkdir()
        (sub / "track.mp3").write_bytes(b"\x00" * 50)

        h1 = compute_folder_hash(tmp_path, child_hashes=["child_hash_1"])
        h2 = compute_folder_hash(tmp_path, child_hashes=["child_hash_2"])
        assert h1 != h2


class TestCoalescePaths:
    def test_parent_subsumes_child(self) -> None:
        paths = ["/music/jazz", "/music/jazz/miles", "/music/jazz/coltrane"]
        result = coalesce_paths(paths)
        assert result == ["/music/jazz"]

    def test_siblings_preserved(self) -> None:
        paths = ["/music/jazz", "/music/rock"]
        result = coalesce_paths(paths)
        assert set(result) == {"/music/jazz", "/music/rock"}

    def test_empty_list(self) -> None:
        assert coalesce_paths([]) == []

    def test_single_path(self) -> None:
        assert coalesce_paths(["/music"]) == ["/music"]

    def test_deep_nesting(self) -> None:
        paths = ["/a", "/a/b", "/a/b/c", "/a/b/c/d"]
        result = coalesce_paths(paths)
        assert result == ["/a"]


class TestCanonicalizePath:
    def test_normpath(self) -> None:
        result = canonicalize_path("/music//jazz/../jazz/")
        assert "//" not in result
        assert result.endswith("jazz")

    def test_consistent(self) -> None:
        a = canonicalize_path("/music/jazz")
        b = canonicalize_path("/music/jazz/")
        assert a == b


class TestDiffTree:
    def test_first_run_returns_empty_changes(self, tmp_path: Path) -> None:
        """First run builds the tree but reports no changes."""
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        changes, pending = diff_tree(str(tmp_path), repo)

        # First run: no stored hashes to diff against
        assert changes == []
        # But the repo should now have folder entries
        assert repo.has_any() is True

    def test_detects_new_file(self, tmp_path: Path) -> None:
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        # First run: populate
        diff_tree(str(tmp_path), repo)

        # Add a new file
        (sub / "track2.flac").write_bytes(b"\x00" * 200)

        changes, pending = diff_tree(str(tmp_path), repo)
        assert len(changes) > 0
        # The jazz folder should be in the changed list
        changed_paths = [c for c in changes]
        assert any("jazz" in p for p in changed_paths)

    def test_no_changes_returns_empty(self, tmp_path: Path) -> None:
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        diff_tree(str(tmp_path), repo)

        # Simulate committing the hashes (as scan would do)
        for folder in repo.get_all():
            if folder.folder_hash is None:
                continue

        # Second run, no changes
        changes, pending = diff_tree(str(tmp_path), repo)
        assert changes == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_folder_hash_service.py -v`

Expected: `ModuleNotFoundError: No module named 'backend.services.folder_hash_service'`

- [ ] **Step 3: Implement folder_hash_service.py**

Create `backend/services/folder_hash_service.py`:

```python
"""
Folder hash service — Merkle-tree-like change detection for library directories.

Uses mtime + file size (not content hashing) for fast change detection.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path
from uuid import uuid4

import structlog

from backend.repositories.library_folders import LibraryFolderRepository
from backend.domain.models import LibraryFolder
from backend.services.library_scan_service import SUPPORTED_EXTENSIONS

logger = structlog.get_logger()


def canonicalize_path(path: str) -> str:
    """Normalize a path for consistent DB storage and comparison."""
    normalized = os.path.normpath(path)
    # NFC unicode normalization
    normalized = unicodedata.normalize("NFC", normalized)
    return normalized


def compute_folder_hash(
    folder_path: Path,
    child_hashes: list[str] | None = None,
) -> str:
    """Compute a hash for a folder based on mtime+size of audio files and child hashes.

    Args:
        folder_path: Path to the directory.
        child_hashes: Pre-computed hashes of child folders (sorted).

    Returns:
        SHA-256 hex digest representing the folder's current state.
    """
    file_parts: list[str] = []
    try:
        for entry in sorted(os.scandir(folder_path), key=lambda e: e.name):
            if not entry.is_file():
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            stat = entry.stat()
            file_parts.append(f"{entry.name}:{stat.st_mtime}:{stat.st_size}")
    except OSError:
        pass

    parts = sorted(child_hashes or []) + sorted(file_parts)
    combined = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def coalesce_paths(paths: list[str]) -> list[str]:
    """Merge child paths into parent paths where the parent is also in the list.

    If both /music/jazz and /music/jazz/miles are changed, keep only /music/jazz.
    """
    if not paths:
        return []

    sorted_paths = sorted(paths)
    result: list[str] = []

    for path in sorted_paths:
        normalized = path.rstrip("/")
        # Check if any already-added path is a parent
        if any(normalized.startswith(r.rstrip("/") + "/") for r in result):
            continue
        result.append(normalized)

    return result


def diff_tree(
    root_path: str,
    folder_repo: LibraryFolderRepository,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Walk the directory tree and find folders whose hash has changed.

    Args:
        root_path: Root directory to walk.
        folder_repo: Repository for stored folder hashes.

    Returns:
        A tuple of (changed_folder_paths, pending_hashes) where
        pending_hashes is a list of (full_path, new_hash) for all folders.
    """
    root = Path(canonicalize_path(root_path))
    if not root.is_dir():
        logger.warning("diff_tree_root_not_found", root=str(root))
        return [], []

    is_first_run = not folder_repo.has_any()

    # Build folder structure bottom-up using os.walk
    # os.walk with topdown=False gives us children before parents
    folder_hashes: dict[str, str] = {}
    all_dirs: list[str] = []

    for dirpath, dirnames, _filenames in os.walk(str(root), topdown=False):
        canonical = canonicalize_path(dirpath)
        all_dirs.append(canonical)

        # Collect child folder hashes
        child_hashes = []
        for d in sorted(dirnames):
            child_path = canonicalize_path(os.path.join(dirpath, d))
            if child_path in folder_hashes:
                child_hashes.append(folder_hashes[child_path])

        folder_hash = compute_folder_hash(Path(dirpath), child_hashes)
        folder_hashes[canonical] = folder_hash

    # Ensure all folders exist in the repo
    # Build path -> folder mapping
    existing_folders: dict[str, LibraryFolder] = {}
    for folder in folder_repo.get_all():
        existing_folders[folder.full_path] = folder

    # Create missing folder entries
    for dir_path in all_dirs:
        if dir_path not in existing_folders:
            parent_path = canonicalize_path(str(Path(dir_path).parent))
            parent = existing_folders.get(parent_path)
            folder = LibraryFolder(
                id=uuid4(),
                name=Path(dir_path).name,
                full_path=dir_path,
                parent_id=parent.id if parent else None,
                folder_hash=folder_hashes[dir_path] if is_first_run else None,
            )
            folder_repo.upsert(folder)
            existing_folders[dir_path] = folder

    # On first run, set all hashes and return no changes
    if is_first_run:
        for dir_path, new_hash in folder_hashes.items():
            folder = existing_folders[dir_path]
            folder_repo.update_hash(folder.id, new_hash)
        logger.info("diff_tree_first_run", folders=len(all_dirs))
        return [], []

    # Diff: find folders where hash changed
    changed: list[str] = []
    pending: list[tuple[str, str]] = []

    for dir_path, new_hash in folder_hashes.items():
        folder = existing_folders.get(dir_path)
        if folder is None:
            # New folder — always changed
            changed.append(dir_path)
            pending.append((dir_path, new_hash))
        elif folder.folder_hash != new_hash:
            changed.append(dir_path)
            pending.append((dir_path, new_hash))
        else:
            # Hash matches — no change
            pending.append((dir_path, new_hash))

    logger.info(
        "diff_tree_complete",
        total_folders=len(all_dirs),
        changed_folders=len(changed),
    )
    return changed, pending
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_folder_hash_service.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/folder_hash_service.py tests/services/test_folder_hash_service.py
git commit -m "feat: add folder hash service with mtime+size diffing and path coalescing"
```

---

### Task 6: Smart Scan Service

**Files:**
- Modify: `backend/services/library_scan_service.py`
- Create: `tests/services/test_smart_scan.py`

- [ ] **Step 1: Write the failing tests for all 6 scenarios**

Create `tests/services/test_smart_scan.py`:

```python
"""Unit tests for smart per-folder scan (all 6 scenarios)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.models import LibraryFile
from backend.services.library_scan_service import scan_folder_smart
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository


def _make_existing(
    *,
    file_path: str,
    file_hash: str = "existing_hash",
    enrichment_status: EnrichmentStatus = EnrichmentStatus.ENRICHED,
    file_status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash=file_hash,
        format="flac",
        enrichment_status=enrichment_status,
        file_status=file_status,
    )


class TestScanFolderSmartUnchanged:
    """Scenario 1: File present on disk, hash matches DB -> no DB write."""

    @patch("backend.services.library_scan_service._sha256", return_value="existing_hash")
    def test_unchanged_file_not_written(
        self, _mock_sha: object, tmp_path: Path
    ) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        quarantine_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(file_path=str(track))
        file_repo.upsert(existing)

        result = scan_folder_smart(
            folder_path=folder,
            file_repo=file_repo,
            quarantine_repo=quarantine_repo,
        )

        assert result.files_written == 0
        assert result.files_skipped == 1
        # Enrichment status preserved
        f = file_repo.get_by_path(str(track))
        assert f.enrichment_status == EnrichmentStatus.ENRICHED


class TestScanFolderSmartModified:
    """Scenario 2: File present on disk, hash differs -> update, reset enrichment."""

    @patch("backend.services.library_scan_service._sha256", return_value="new_hash")
    def test_modified_file_resets_enrichment(
        self, _mock_sha: object, tmp_path: Path
    ) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        quarantine_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(
            file_path=str(track),
            file_hash="old_hash",
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        file_repo.upsert(existing)

        result = scan_folder_smart(
            folder_path=folder,
            file_repo=file_repo,
            quarantine_repo=quarantine_repo,
        )

        assert result.files_written == 1
        f = file_repo.get_by_path(str(track))
        assert f.file_hash == "new_hash"


class TestScanFolderSmartNew:
    """Scenario 3: File present on disk, no DB record -> insert as PENDING."""

    def test_new_file_inserted(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        quarantine_repo = FakeLibraryQuarantineRepository()

        with patch(
            "backend.services.library_scan_service.extract_tags"
        ) as mock_extract:
            mock_extract.return_value = LibraryFile(
                id=uuid4(),
                file_path=str(track),
                file_hash="brand_new_hash",
                format="flac",
                enrichment_status=EnrichmentStatus.PENDING,
            )
            result = scan_folder_smart(
                folder_path=folder,
                file_repo=file_repo,
                quarantine_repo=quarantine_repo,
            )

        assert result.files_written == 1
        f = file_repo.get_by_path(str(track))
        assert f is not None
        assert f.enrichment_status == EnrichmentStatus.PENDING


class TestScanFolderSmartReappeared:
    """Scenario 4: File present, DB has MISSING status -> restore to PRESENT."""

    @patch("backend.services.library_scan_service._sha256", return_value="existing_hash")
    def test_reappeared_same_hash_preserves_enrichment(
        self, _mock_sha: object, tmp_path: Path
    ) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        quarantine_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(
            file_path=str(track),
            file_status=FileStatus.MISSING,
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        file_repo.upsert(existing)

        result = scan_folder_smart(
            folder_path=folder,
            file_repo=file_repo,
            quarantine_repo=quarantine_repo,
        )

        assert result.files_reappeared == 1
        f = file_repo.get_by_path(str(track))
        assert f.file_status == FileStatus.PRESENT
        assert f.enrichment_status == EnrichmentStatus.ENRICHED


class TestScanFolderSmartMissing:
    """Scenario 5: File in DB but not on disk -> mark MISSING."""

    def test_missing_file_marked(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        # No files on disk, but one in DB

        file_repo = FakeLibraryFileRepository()
        quarantine_repo = FakeLibraryQuarantineRepository()
        ghost = _make_existing(
            file_path=str(folder / "ghost.flac"),
            file_status=FileStatus.PRESENT,
        )
        file_repo.upsert(ghost)

        result = scan_folder_smart(
            folder_path=folder,
            file_repo=file_repo,
            quarantine_repo=quarantine_repo,
        )

        assert result.files_missing == 1
        f = file_repo.get_by_path(str(folder / "ghost.flac"))
        assert f.file_status == FileStatus.MISSING
        assert f.enrichment_status == EnrichmentStatus.ENRICHED  # preserved


class TestScanFolderSmartParseFailure:
    """Scenario 6: File present but Mutagen fails -> quarantine."""

    def test_parse_failure_quarantined(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "corrupt.flac"
        track.write_bytes(b"\x00" * 10)

        file_repo = FakeLibraryFileRepository()
        quarantine_repo = FakeLibraryQuarantineRepository()

        from mutagen._util import MutagenError

        with patch(
            "backend.services.library_scan_service.extract_tags",
            side_effect=MutagenError("bad file"),
        ):
            result = scan_folder_smart(
                folder_path=folder,
                file_repo=file_repo,
                quarantine_repo=quarantine_repo,
            )

        assert result.quarantined == 1
        q = quarantine_repo.get_by_path(str(track))
        assert q is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_smart_scan.py -v`

Expected: `ImportError: cannot import name 'scan_folder_smart'`

- [ ] **Step 3: Implement scan_folder_smart**

Add to `backend/services/library_scan_service.py` (at the end, after `scan_directory`):

```python
@dataclass
class SmartScanResult:
    """Result counts from a smart per-folder scan."""
    files_written: int = 0
    files_skipped: int = 0
    files_missing: int = 0
    files_reappeared: int = 0
    quarantined: int = 0


def scan_folder_smart(
    *,
    folder_path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
) -> SmartScanResult:
    """Smart scan of a single folder — diffs disk vs DB, handles all 6 scenarios.

    Only processes files directly in this folder (not recursive).
    """
    result = SmartScanResult()

    # Get existing DB records for this folder
    existing_by_path: dict[str, LibraryFile] = {
        f.file_path: f
        for f in file_repo.get_by_folder_path(str(folder_path))
    }

    # Get files on disk
    disk_files: dict[str, Path] = {}
    if folder_path.is_dir():
        for entry in folder_path.iterdir():
            if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                disk_files[str(entry)] = entry

    # Process files on disk
    for file_path_str, path in disk_files.items():
        existing = existing_by_path.pop(file_path_str, None)

        if existing is not None:
            # File exists in DB — check hash
            try:
                current_hash = _sha256(path)
            except OSError:
                result.quarantined += 1
                continue

            if existing.file_status == FileStatus.MISSING:
                # Re-appeared file
                if current_hash == existing.file_hash:
                    # Same content — just restore status
                    existing.file_status = FileStatus.PRESENT
                    file_repo.upsert(existing)
                else:
                    # Content changed — re-extract tags
                    try:
                        lf = extract_tags(path)
                        file_repo.upsert(lf)
                        result.files_written += 1
                    except (MutagenError, Exception):
                        _quarantine_file(path, quarantine_repo, result)
                        continue
                result.files_reappeared += 1
            elif current_hash == existing.file_hash:
                # Unchanged — skip
                result.files_skipped += 1
            else:
                # Modified — re-extract and upsert (hash change triggers enrichment reset)
                try:
                    lf = extract_tags(path)
                    file_repo.upsert(lf)
                    result.files_written += 1
                except (MutagenError, Exception):
                    _quarantine_file(path, quarantine_repo, result)
        else:
            # New file — extract tags and insert
            try:
                lf = extract_tags(path)
                file_repo.upsert(lf)
                result.files_written += 1
            except (MutagenError, Exception):
                _quarantine_file(path, quarantine_repo, result)

    # Remaining entries in existing_by_path are files in DB but not on disk
    for file_path_str, existing in existing_by_path.items():
        if existing.file_status == FileStatus.PRESENT:
            file_repo.mark_missing(file_path_str)
            result.files_missing += 1

    return result


def _quarantine_file(
    path: Path,
    quarantine_repo: LibraryQuarantineRepository,
    result: SmartScanResult,
) -> None:
    """Create a quarantine entry for a file that failed to parse."""
    import traceback
    entry = LibraryQuarantine(
        id=uuid4(),
        file_path=str(path),
        error_message=traceback.format_exc(),
    )
    quarantine_repo.create_write_only(entry)
    result.quarantined += 1
```

Also add imports at the top of the file:

```python
from dataclasses import dataclass

from backend.domain.enums import FileStatus
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.library_quarantine import LibraryQuarantineRepository
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_smart_scan.py -v`

Expected: All 6 scenario tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/library_scan_service.py tests/services/test_smart_scan.py
git commit -m "feat: add scan_folder_smart with 6-scenario per-folder diffing"
```

---

### Task 7: Enrichment Chaining on Existing Scan Task

**Files:**
- Modify: `backend/tasks/library_tasks.py:103-199`

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_scan_enrichment_chain.py`:

```python
"""Test that library_scan_task chains into enrichment on completion."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestScanEnrichmentChain:
    @patch("backend.tasks.library_tasks.library_enrichment_task")
    @patch("backend.tasks.library_tasks._run_scan")
    @patch("backend.tasks.library_tasks.psycopg")
    def test_chains_enrichment_when_files_written(
        self,
        mock_psycopg: MagicMock,
        mock_run_scan: MagicMock,
        mock_enrichment: MagicMock,
    ) -> None:
        # Mock connection context
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        # _run_scan returns (files_written=5, quarantine=0, progress)
        mock_run_scan.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})

        from backend.tasks.library_tasks import library_scan_task

        # Call the underlying function directly (not via Huey)
        library_scan_task.call_local("/music")

        mock_enrichment.assert_called_once()

    @patch("backend.tasks.library_tasks.library_enrichment_task")
    @patch("backend.tasks.library_tasks._run_scan")
    @patch("backend.tasks.library_tasks.psycopg")
    def test_no_enrichment_when_zero_files(
        self,
        mock_psycopg: MagicMock,
        mock_run_scan: MagicMock,
        mock_enrichment: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_run_scan.return_value = (0, 0, {"processed": 0, "total": 0, "current_path": ""})

        from backend.tasks.library_tasks import library_scan_task

        library_scan_task.call_local("/empty")

        mock_enrichment.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tasks/test_scan_enrichment_chain.py -v`

Expected: `mock_enrichment.assert_called_once()` fails (not called).

- [ ] **Step 3: Add enrichment chaining to library_scan_task**

In `backend/tasks/library_tasks.py`, add after the `logger.info("library_scan_task_complete", ...)` block (line 171), before the `except` clause:

```python
        # Fire-and-forget: chain into enrichment if any files were written
        if files_written > 0:
            from backend.tasks.library_enrichment_tasks import library_enrichment_task
            library_enrichment_task()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/tasks/test_scan_enrichment_chain.py -v`

Expected: Both tests PASS.

- [ ] **Step 5: Run existing scan tests to verify no regressions**

Run: `uv run pytest tests/tasks/test_library_tasks.py -v`

Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/tasks/library_tasks.py tests/tasks/test_scan_enrichment_chain.py
git commit -m "feat: chain library_scan_task into enrichment when files_written > 0"
```

---

### Task 8: Watcher Poll Task and Targeted Scan Task

**Files:**
- Create: `backend/tasks/library_watcher_tasks.py`
- Modify: `backend/tasks/huey_app.py:17-23`
- Create: `tests/tasks/test_watcher_tasks.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tasks/test_watcher_tasks.py`:

```python
"""Unit tests for library_watcher_poll and library_scan_files_task."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestWatcherPollNoPath:
    """Watcher should no-op when local_path_prefix is not set."""

    @patch("backend.tasks.library_watcher_tasks.psycopg")
    def test_noop_when_no_path(self, mock_psycopg: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        # Settings repo returns None for local_path_prefix
        mock_settings = MagicMock()
        mock_settings.get.return_value = None

        with patch(
            "backend.tasks.library_watcher_tasks.RepositoryFactory"
        ) as mock_factory:
            mock_factory.return_value.settings = mock_settings
            with patch(
                "backend.tasks.library_watcher_tasks.diff_tree"
            ) as mock_diff:
                from backend.tasks.library_watcher_tasks import (
                    library_watcher_poll,
                )

                library_watcher_poll.call_local()

                mock_diff.assert_not_called()


class TestWatcherPollNoChanges:
    """Watcher should return early when no changes detected."""

    @patch("backend.tasks.library_watcher_tasks.psycopg")
    def test_no_scan_when_no_changes(self, mock_psycopg: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_settings = MagicMock()
        mock_settings.get.return_value = "/music"

        with patch(
            "backend.tasks.library_watcher_tasks.RepositoryFactory"
        ) as mock_factory:
            mock_factory.return_value.settings = mock_settings
            with patch(
                "backend.tasks.library_watcher_tasks.diff_tree",
                return_value=([], []),
            ):
                with patch(
                    "backend.tasks.library_watcher_tasks.library_scan_files_task"
                ) as mock_scan:
                    from backend.tasks.library_watcher_tasks import (
                        library_watcher_poll,
                    )

                    library_watcher_poll.call_local()

                    mock_scan.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tasks/test_watcher_tasks.py -v`

Expected: `ModuleNotFoundError: No module named 'backend.tasks.library_watcher_tasks'`

- [ ] **Step 3: Implement library_watcher_tasks.py**

Create `backend/tasks/library_watcher_tasks.py`:

```python
"""
Library watcher tasks — periodic polling and targeted smart scan.

The poll task runs every 4 minutes, diffs folder hashes, and enqueues a
targeted scan for changed folders. The scan task processes changed folders
using smart per-folder diffing and chains into enrichment.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import structlog
from huey import crontab  # type: ignore[import-untyped]
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.progress_tracking import PgProgressTrackingRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import ProgressTracking
from backend.services.folder_hash_service import coalesce_paths, diff_tree
from backend.services.library_scan_service import scan_folder_smart
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.periodic_task(crontab(minute="*/4"))  # type: ignore[untyped-decorator]
def library_watcher_poll() -> None:
    """Poll the library directory for changes every 4 minutes."""
    settings = get_settings()

    with psycopg.connect(
        settings.database_url, autocommit=False, row_factory=dict_row
    ) as conn:
        repos = RepositoryFactory(conn)

        root_path = repos.settings.get("local_path_prefix")
        if not root_path:
            return

        changed, pending = diff_tree(root_path, repos.library_folders)
        conn.commit()  # Persist any new folder entries from first-run init

        if not changed:
            return

        coalesced = coalesce_paths(changed)

        # Stage pending hashes
        task_id = uuid.uuid4().hex
        folder_id_map = {
            f.full_path: f.id for f in repos.library_folders.get_all()
        }
        hashes_to_stage = [
            (folder_id_map[path], new_hash)
            for path, new_hash in pending
            if path in folder_id_map
        ]
        repos.library_folders.stage_hashes(hashes_to_stage, task_id)
        conn.commit()

        logger.info(
            "watcher_poll_changes_detected",
            changed=len(coalesced),
            task_id=task_id,
        )

        # Fire-and-forget
        library_scan_files_task(coalesced, task_id)


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_files_task(
    folder_paths: list[str], task_id: str
) -> None:
    """Smart scan of specific folders, then chain into enrichment."""
    settings = get_settings()
    scan_task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    total_written = 0

    progress_conn = None
    library_conn = None

    try:
        progress_conn = psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        )
        progress_repo = PgProgressTrackingRepository(progress_conn)

        library_conn = psycopg.connect(
            settings.database_url, autocommit=False, row_factory=dict_row
        )

        # Advisory lock
        lock_key = folder_paths[0] if folder_paths else "watcher"
        lock_acquired = library_conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)::bigint)",
            (lock_key,),
        ).fetchone()
        if not lock_acquired or not lock_acquired[0]:
            logger.warning("scan_lock_held", paths=folder_paths)
            return

        repos = RepositoryFactory(library_conn)

        # Initial progress
        progress_repo.upsert(
            ProgressTracking(
                task_id=scan_task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data={
                    "processed": 0,
                    "total": len(folder_paths),
                    "current_path": "",
                    "source": "watcher",
                },
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        for idx, folder_path in enumerate(folder_paths, start=1):
            result = scan_folder_smart(
                folder_path=Path(folder_path),
                file_repo=repos.library_files,
                quarantine_repo=repos.library_quarantine,
            )
            total_written += result.files_written
            library_conn.commit()

            progress_repo.upsert(
                ProgressTracking(
                    task_id=scan_task_id,
                    task_type=TaskType.SCAN,
                    status=TaskStatus.RUNNING,
                    progress_data={
                        "processed": idx,
                        "total": len(folder_paths),
                        "current_path": folder_path,
                        "source": "watcher",
                        "files_written": total_written,
                    },
                    started_at=task_started_at,
                    updated_at=datetime.now(UTC),
                )
            )

            logger.info(
                "watcher_scan_folder_complete",
                folder=folder_path,
                written=result.files_written,
                skipped=result.files_skipped,
                missing=result.files_missing,
                reappeared=result.files_reappeared,
                quarantined=result.quarantined,
            )

        # Commit staged hashes on success
        repos.library_folders.commit_staged_hashes(task_id)
        library_conn.commit()

        # Mark completed
        progress_repo.upsert(
            ProgressTracking(
                task_id=scan_task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.COMPLETED,
                progress_data={
                    "processed": len(folder_paths),
                    "total": len(folder_paths),
                    "source": "watcher",
                    "files_written": total_written,
                },
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )

        logger.info(
            "watcher_scan_complete",
            folders=len(folder_paths),
            total_written=total_written,
        )

        # Chain into enrichment
        if total_written > 0:
            from backend.tasks.library_enrichment_tasks import (
                library_enrichment_task,
            )

            library_enrichment_task()

    except Exception as exc:
        if library_conn is not None:
            with contextlib.suppress(Exception):
                library_conn.rollback()

        if progress_conn is not None:
            with contextlib.suppress(Exception):
                PgProgressTrackingRepository(progress_conn).upsert(
                    ProgressTracking(
                        task_id=scan_task_id,
                        task_type=TaskType.SCAN,
                        status=TaskStatus.FAILED,
                        progress_data={
                            "error": str(exc),
                            "source": "watcher",
                        },
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )

        logger.error("watcher_scan_failed", error=str(exc))
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()
```

- [ ] **Step 4: Register in huey_app.py**

Add to `backend/tasks/huey_app.py` after the last import:

```python
import backend.tasks.library_watcher_tasks  # noqa: F401, E402
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/tasks/test_watcher_tasks.py -v`

Expected: Both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/tasks/library_watcher_tasks.py backend/tasks/huey_app.py tests/tasks/test_watcher_tasks.py
git commit -m "feat: add watcher poll task (4-min crontab) and targeted scan task with enrichment chaining"
```

---

### Task 9: Integration Test — Full Watcher Poll Cycle

**Files:**
- Create: `tests/integration/test_watcher_poll.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_watcher_poll.py`:

```python
"""Integration test: full watcher poll cycle with real DB."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_folders import PgLibraryFolderRepository
from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.models import LibraryFile
from backend.services.folder_hash_service import diff_tree


def test_first_run_builds_tree_no_changes(
    migrated_db: str, tmp_path: Path
) -> None:
    """First diff_tree call builds folder structure, returns no changes."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    (jazz / "track.flac").write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        changes, pending = diff_tree(str(tmp_path), repo)
        conn.commit()

        assert changes == []
        assert repo.has_any() is True


def test_second_run_detects_new_file(
    migrated_db: str, tmp_path: Path
) -> None:
    """After first run, adding a file triggers a change on second run."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    (jazz / "track1.flac").write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)

        # First run
        diff_tree(str(tmp_path), repo)
        conn.commit()

        # Add a new file
        (jazz / "track2.flac").write_bytes(b"\x00" * 200)

        # Second run
        changes, pending = diff_tree(str(tmp_path), repo)
        conn.commit()

        assert len(changes) > 0


def test_upsert_preserves_enrichment_in_full_cycle(
    migrated_db: str, tmp_path: Path
) -> None:
    """An already-enriched file should keep its status after a full scan cycle."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    track = jazz / "track.flac"
    track.write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        file_repo = PgLibraryFileRepository(conn)

        # Insert an enriched file
        lf = LibraryFile(
            id=uuid4(),
            file_path=str(track),
            file_hash="abc123",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        file_repo.upsert(lf)
        conn.commit()

        # Re-upsert with same hash (as watcher scan would do)
        lf2 = LibraryFile(
            id=uuid4(),
            file_path=str(track),
            file_hash="abc123",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
        )
        file_repo.upsert_write_only(lf2)
        conn.commit()

        result = file_repo.get_by_path(str(track))
        assert result is not None
        assert result.enrichment_status == EnrichmentStatus.ENRICHED
```

- [ ] **Step 2: Run integration tests**

Run: `uv run pytest tests/integration/test_watcher_poll.py -v`

Expected: All 3 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_watcher_poll.py
git commit -m "test: add integration tests for full watcher poll cycle"
```

---

### Task 10: Run Full Test Suite and Final Verification

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest -x -v`

Expected: All tests PASS with no failures.

- [ ] **Step 2: Verify migration applies to a fresh database**

Run: `uv run pytest tests/integration/test_pg_library_folders.py tests/integration/test_upsert_enrichment_preservation.py tests/integration/test_watcher_poll.py -v`

Expected: All integration tests PASS (they use the `migrated_db` fixture which runs all migrations).

- [ ] **Step 3: Verify Huey consumer can import the new task module**

Run: `uv run python -c "from backend.tasks.huey_app import huey; print(f'Tasks: {list(huey._registry._registry.keys())}')"`

Expected: Output includes `library_watcher_poll` and `library_scan_files_task` in the registry.

- [ ] **Step 4: Commit if any fixes were needed**

Only if Step 1-3 revealed issues that needed fixes:
```bash
git add -u
git commit -m "fix: address issues found in final verification"
```
