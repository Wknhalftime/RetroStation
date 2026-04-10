# Scan Library Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Scan Library" button to the Library and Path Configuration pages, wire the scan task to progress tracking, and add a sidebar activity indicator.

**Architecture:** Backend refactors `scan_directory()` to accept a progress callback and rewrites `library_scan_task()` to create/update `progress_tracking` records via a separate autocommit connection. Frontend adds a shared `ScanLibraryButton` component, expands `progressStore` with a `runningTasks` collection, and adds pulsing dots to the Sidebar.

**Tech Stack:** Python (psycopg, mutagen, Huey), TypeScript (React, TanStack Query, Zustand, Tailwind CSS)

**Spec:** `docs/superpowers/specs/2026-04-03-scan-button-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `backend/services/library_scan_service.py` | Add `on_progress` callback to `scan_directory()`, pre-count pass, throttled callbacks |
| Modify | `backend/tasks/library_tasks.py` | Progress tracking lifecycle (create/update/complete/fail) with autocommit connection |
| Modify | `frontend/src/api/library.ts` | Add `useScanLibrary()` mutation |
| Modify | `frontend/src/store/progressStore.ts` | Add `runningTasks` collection + `hasRunningType()` getter |
| Create | `frontend/src/components/domain/library/ScanLibraryButton.tsx` | Shared scan button with 4 states |
| Modify | `frontend/src/pages/library/LibraryStatus.tsx` | Add `ScanLibraryButton` to page header actions |
| Modify | `frontend/src/pages/settings/PathConfiguration.tsx` | Add `ScanLibraryButton` to footer row |
| Modify | `frontend/src/components/layout/Sidebar.tsx` | Add pulsing activity dot per nav item |
| Create | `tests/services/test_scan_progress.py` | Tests for `scan_directory()` callback and `library_scan_task()` progress tracking |

---

## Task 1: Add `on_progress` Callback to `scan_directory()`

**Files:**
- Modify: `backend/services/library_scan_service.py:298-338`
- Create: `tests/services/test_scan_progress.py`

- [ ] **Step 1a: Write failing test for callback firing**

Create `tests/services/test_scan_progress.py`:

```python
"""Tests for scan_directory progress callback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.services.library_scan_service import scan_directory

AUDIO_DIR = Path(__file__).parent.parent / "fixtures" / "audio"
NO_TAGS_WAV = AUDIO_DIR / "no_tags.wav"


def _require_wav() -> Path:
    if not NO_TAGS_WAV.exists():
        pytest.skip("Fixture not found: no_tags.wav")
    return NO_TAGS_WAV


class TestScanDirectoryProgress:
    def test_callback_fires_with_correct_total(self, tmp_path: Path) -> None:
        """Callback receives total == number of supported audio files."""
        wav = _require_wav()
        for i in range(3):
            shutil.copy(wav, tmp_path / f"track_{i}.wav")

        calls: list[tuple[int, int, str]] = []
        scan_directory(tmp_path, on_progress=lambda p, t, c: calls.append((p, t, c)))

        # With 3 files (< 50), callback fires once on the last file
        assert len(calls) == 1
        processed, total, _ = calls[0]
        assert total == 3
        assert processed == 3

    def test_callback_fires_every_50_files(self, tmp_path: Path) -> None:
        """Callback fires at file 50 and at the end for 75 files."""
        wav = _require_wav()
        for i in range(75):
            shutil.copy(wav, tmp_path / f"track_{i:03d}.wav")

        calls: list[tuple[int, int, str]] = []
        scan_directory(tmp_path, on_progress=lambda p, t, c: calls.append((p, t, c)))

        assert len(calls) == 2  # at 50 and at 75
        assert calls[0][0] == 50
        assert calls[0][1] == 75
        assert calls[1][0] == 75
        assert calls[1][1] == 75

    def test_callback_current_path_is_string(self, tmp_path: Path) -> None:
        wav = _require_wav()
        shutil.copy(wav, tmp_path / "single.wav")

        calls: list[tuple[int, int, str]] = []
        scan_directory(tmp_path, on_progress=lambda p, t, c: calls.append((p, t, c)))

        assert len(calls) == 1
        assert isinstance(calls[0][2], str)
        assert "single.wav" in calls[0][2]

    def test_no_callback_is_fine(self, tmp_path: Path) -> None:
        """scan_directory still works without on_progress."""
        wav = _require_wav()
        shutil.copy(wav, tmp_path / "track.wav")

        files, quarantine = scan_directory(tmp_path)
        assert len(files) == 1
        assert len(quarantine) == 0

    def test_results_unchanged_with_callback(self, tmp_path: Path) -> None:
        """Adding a callback does not change the returned files/quarantine."""
        wav = _require_wav()
        for i in range(3):
            shutil.copy(wav, tmp_path / f"track_{i}.wav")

        files_no_cb, q_no_cb = scan_directory(tmp_path)
        files_cb, q_cb = scan_directory(
            tmp_path, on_progress=lambda p, t, c: None
        )

        assert len(files_cb) == len(files_no_cb)
        assert len(q_cb) == len(q_no_cb)

    def test_candidates_are_sorted(self, tmp_path: Path) -> None:
        """Files are processed in sorted order (deterministic)."""
        wav = _require_wav()
        shutil.copy(wav, tmp_path / "z_last.wav")
        shutil.copy(wav, tmp_path / "a_first.wav")

        paths_seen: list[str] = []
        scan_directory(
            tmp_path,
            on_progress=lambda p, t, c: paths_seen.append(c),
        )

        # Only 2 files (< 50), so callback fires once on last file
        # But we can check the returned files are sorted
        files, _ = scan_directory(tmp_path)
        file_names = [Path(f.file_path).name for f in files]
        assert file_names == sorted(file_names)
```

- [ ] **Step 1b: Run tests to verify they fail**

Run: `cd D:/PythonStuff/RetroStation && python -m pytest tests/services/test_scan_progress.py -v`

Expected: FAIL — `scan_directory()` does not accept `on_progress` parameter (TypeError).

- [ ] **Step 1c: Implement `on_progress` in `scan_directory()`**

Modify `backend/services/library_scan_service.py`. Replace the `scan_directory` function (lines 298-338) with:

```python
def scan_directory(
    root: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[LibraryFile], list[LibraryQuarantine]]:
    """
    Walk *root* recursively and extract tags from all supported audio files.

    Returns ``(files, quarantine)`` where *quarantine* contains an entry for
    every file that raised a :exc:`mutagen.MutagenError`.

    If *on_progress* is provided, it is called with ``(processed, total,
    current_path)`` every 50 files and on the final file.
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
        except MutagenError as exc:
            logger.warning("Quarantining %s: %s", path, exc)
            quarantine.append(
                LibraryQuarantine(
                    id=uuid4(),
                    file_path=str(path),
                    error_message=str(exc),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error scanning %s: %s", path, exc)
            quarantine.append(
                LibraryQuarantine(
                    id=uuid4(),
                    file_path=str(path),
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            )

        if on_progress is not None and (
            processed_idx % 50 == 0 or processed_idx == total
        ):
            on_progress(processed_idx, total, str(path))

    return files, quarantine
```

Also add `Callable` to the typing import at line 16:

```python
from typing import Any, Callable
```

And update the module docstring at line 5:

```python
  scan_directory(root, on_progress=None)  -> (list[LibraryFile], list[LibraryQuarantine])
```

- [ ] **Step 1d: Run tests to verify they pass**

Run: `cd D:/PythonStuff/RetroStation && python -m pytest tests/services/test_scan_progress.py -v`

Expected: All 6 tests PASS.

- [ ] **Step 1e: Run existing scan tests to verify no regressions**

Run: `cd D:/PythonStuff/RetroStation && python -m pytest tests/services/test_library_scan.py -v`

Expected: All existing tests PASS (the `on_progress` parameter defaults to `None`).

- [ ] **Step 1f: Run mypy**

Run: `cd D:/PythonStuff/RetroStation && python -m mypy backend/services/library_scan_service.py --strict`

Expected: Clean (0 errors).

- [ ] **Step 1g: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add backend/services/library_scan_service.py tests/services/test_scan_progress.py
git commit -m "feat(scan): add on_progress callback to scan_directory()"
```

---

## Task 2: Wire `library_scan_task()` to Progress Tracking

**Files:**
- Modify: `backend/tasks/library_tasks.py`
- Modify: `tests/services/test_scan_progress.py` (add task-level tests)

- [ ] **Step 2a: Write failing tests for task progress tracking**

Append to `tests/services/test_scan_progress.py`:

```python
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import LibraryFile, LibraryQuarantine, TaskProgress
from tests.fakes.progress_tracking import FakeTaskProgressRepository


class TestLibraryScanTaskProgress:
    """Tests for library_scan_task progress tracking lifecycle."""

    def _make_fake_scan(
        self, file_count: int = 3
    ) -> MagicMock:
        """Return a mock scan_directory that returns N files and 0 quarantine."""
        files = [
            MagicMock(spec=LibraryFile) for _ in range(file_count)
        ]
        mock = MagicMock(return_value=(files, []))
        return mock

    @patch("backend.tasks.library_tasks.psycopg")
    @patch("backend.tasks.library_tasks.scan_directory")
    def test_creates_running_record_before_scan(
        self, mock_scan: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        from backend.tasks.library_tasks import library_scan_task

        fake_progress = FakeTaskProgressRepository()
        mock_scan.return_value = ([], [])

        # Mock the autocommit connection to use our fake repo
        mock_autocommit_conn = MagicMock()
        mock_data_conn = MagicMock()
        mock_data_conn.__enter__ = MagicMock(return_value=mock_data_conn)
        mock_data_conn.__exit__ = MagicMock(return_value=False)

        call_count = 0
        def connect_side_effect(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if kwargs.get("autocommit"):
                return mock_autocommit_conn
            return mock_data_conn

        mock_psycopg.connect.side_effect = connect_side_effect

        with patch(
            "backend.tasks.library_tasks.PgTaskProgressRepository",
            return_value=fake_progress,
        ):
            library_scan_task("/fake/path")

        # Verify a running record was created
        records = list(fake_progress._data.values())
        assert len(records) == 1
        assert records[0].status == TaskStatus.COMPLETED
        assert records[0].task_type == TaskType.SCAN

    @patch("backend.tasks.library_tasks.psycopg")
    @patch("backend.tasks.library_tasks.scan_directory")
    def test_marks_failed_on_scan_exception(
        self, mock_scan: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        from backend.tasks.library_tasks import library_scan_task

        fake_progress = FakeTaskProgressRepository()
        mock_scan.side_effect = RuntimeError("disk error")

        mock_autocommit_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_autocommit_conn

        with (
            patch(
                "backend.tasks.library_tasks.PgTaskProgressRepository",
                return_value=fake_progress,
            ),
            pytest.raises(RuntimeError, match="disk error"),
        ):
            library_scan_task("/fake/path")

        records = list(fake_progress._data.values())
        assert len(records) == 1
        assert records[0].status == TaskStatus.FAILED
        assert "disk error" in records[0].progress_data.get("error", "")

    @patch("backend.tasks.library_tasks.psycopg")
    @patch("backend.tasks.library_tasks.scan_directory")
    def test_progress_data_has_processed_and_total(
        self, mock_scan: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        from backend.tasks.library_tasks import library_scan_task

        fake_progress = FakeTaskProgressRepository()
        mock_scan.return_value = ([], [])

        mock_autocommit_conn = MagicMock()
        mock_data_conn = MagicMock()
        mock_data_conn.__enter__ = MagicMock(return_value=mock_data_conn)
        mock_data_conn.__exit__ = MagicMock(return_value=False)

        def connect_side_effect(*args: object, **kwargs: object) -> object:
            if kwargs.get("autocommit"):
                return mock_autocommit_conn
            return mock_data_conn

        mock_psycopg.connect.side_effect = connect_side_effect

        with patch(
            "backend.tasks.library_tasks.PgTaskProgressRepository",
            return_value=fake_progress,
        ):
            library_scan_task("/fake/path")

        records = list(fake_progress._data.values())
        assert "processed" in records[0].progress_data
        assert "total" in records[0].progress_data
```

- [ ] **Step 2b: Run tests to verify they fail**

Run: `cd D:/PythonStuff/RetroStation && python -m pytest tests/services/test_scan_progress.py::TestLibraryScanTaskProgress -v`

Expected: FAIL — `library_scan_task` does not import/use `PgTaskProgressRepository`.

- [ ] **Step 2c: Implement progress tracking in `library_scan_task()`**

Replace the full contents of `backend/tasks/library_tasks.py` with:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.progress_tracking import PgTaskProgressRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import TaskProgress
from backend.services.library_scan_service import scan_directory
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_task(root_path: str) -> str:
    """Scan a directory for audio files and persist results to the DB."""
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    # Separate autocommit connection for progress tracking.
    # Intentional layer skip past RepositoryFactory — progress writes must be
    # visible immediately (not held in the library data transaction).
    progress_conn = None
    progress_repo: PgTaskProgressRepository | None = None
    try:
        progress_conn = psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        )
        progress_repo = PgTaskProgressRepository(progress_conn)

        # Initial record
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

        # Callback — closes over task_id, task_started_at, last_progress,
        # progress_repo
        def on_progress(processed: int, total: int, current_path: str) -> None:
            nonlocal last_progress
            last_progress = {
                "processed": processed,
                "total": total,
                "current_path": current_path,
            }
            assert progress_repo is not None  # for mypy — always true here
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

        files, quarantine = scan_directory(Path(root_path), on_progress=on_progress)

        # Persist library data — main transactional connection
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            repos = RepositoryFactory(conn)
            for lf in files:
                repos.library_files.upsert(lf)
            for entry in quarantine:
                repos.library_quarantine.create(entry)
            conn.commit()

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
            files_indexed=len(files),
            quarantined=len(quarantine),
        )

    except Exception as exc:
        if progress_conn is not None and progress_repo is not None:
            try:
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
            except Exception:
                pass  # Don't mask the original error
        raise

    finally:
        if progress_conn is not None:
            progress_conn.close()

    return root_path
```

- [ ] **Step 2d: Run tests to verify they pass**

Run: `cd D:/PythonStuff/RetroStation && python -m pytest tests/services/test_scan_progress.py -v`

Expected: All 9 tests PASS (6 from Task 1 + 3 new).

- [ ] **Step 2e: Run mypy**

Run: `cd D:/PythonStuff/RetroStation && python -m mypy backend/tasks/library_tasks.py --strict`

Expected: Clean (0 errors).

- [ ] **Step 2f: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add backend/tasks/library_tasks.py tests/services/test_scan_progress.py
git commit -m "feat(scan): wire library_scan_task to progress_tracking table"
```

---

## Task 3: Add `runningTasks` and `hasRunningType()` to `progressStore`

**Files:**
- Modify: `frontend/src/store/progressStore.ts`

- [ ] **Step 3a: Add `runningTasks` and `hasRunningType()` to the store**

Modify `frontend/src/store/progressStore.ts`. Replace the full file with:

```typescript
import { create } from "zustand";
import type { TaskInfo } from "@/lib/schemas/tasks";

export type ProgressStatus = "IDLE" | "RUNNING" | "COMPLETED" | "FAILED";

interface ProgressState {
  status: ProgressStatus;
  activeTask: TaskInfo | null;
  extraCount: number;
  runningTasks: TaskInfo[];
  dismissTimer: ReturnType<typeof setTimeout> | null;
  setTasks: (tasks: TaskInfo[]) => void;
  hasRunningType: (type: string) => boolean;
  dismiss: () => void;
}

function pickActiveTask(tasks: TaskInfo[]): TaskInfo | null {
  if (tasks.length === 0) return null;
  // Most recently started task
  return tasks.slice().sort((a, b) =>
    b.started_at.localeCompare(a.started_at)
  )[0] ?? null;
}

export const useProgressStore = create<ProgressState>((set, get) => ({
  status: "IDLE",
  activeTask: null,
  extraCount: 0,
  runningTasks: [],
  dismissTimer: null,

  setTasks: (tasks: TaskInfo[]) => {
    const runningTasks = tasks.filter((t) => t.status === "running");
    const failedTasks = tasks.filter((t) => t.status === "failed");
    const completedTasks = tasks.filter((t) => t.status === "completed");

    const { dismissTimer } = get();

    if (runningTasks.length > 0) {
      // Clear any pending dismiss timer when new tasks arrive
      if (dismissTimer) {
        clearTimeout(dismissTimer);
      }
      const active = pickActiveTask(runningTasks);
      set({
        status: "RUNNING",
        activeTask: active,
        extraCount: Math.max(0, runningTasks.length - 1),
        runningTasks,
        dismissTimer: null,
      });
      return;
    }

    if (failedTasks.length > 0) {
      if (dismissTimer) clearTimeout(dismissTimer);
      const active = pickActiveTask(failedTasks);
      set({
        status: "FAILED",
        activeTask: active,
        extraCount: Math.max(0, failedTasks.length - 1),
        runningTasks: [],
        dismissTimer: null,
      });
      return;
    }

    if (completedTasks.length > 0) {
      const { status } = get();
      // Only transition to COMPLETED if we were previously RUNNING
      if (status === "RUNNING") {
        if (dismissTimer) clearTimeout(dismissTimer);
        const active = pickActiveTask(completedTasks);
        const timer = setTimeout(() => {
          set({ status: "IDLE", activeTask: null, extraCount: 0, runningTasks: [], dismissTimer: null });
        }, 2000);
        set({
          status: "COMPLETED",
          activeTask: active,
          extraCount: Math.max(0, completedTasks.length - 1),
          runningTasks: [],
          dismissTimer: timer,
        });
      }
      return;
    }

    // No tasks: go idle if currently running/completed (not if FAILED — user must dismiss)
    const { status } = get();
    if (status === "RUNNING" || status === "COMPLETED") {
      if (dismissTimer) clearTimeout(dismissTimer);
      set({ status: "IDLE", activeTask: null, extraCount: 0, runningTasks: [], dismissTimer: null });
    }
  },

  hasRunningType: (type: string) => get().runningTasks.some((t) => t.task_type === type),

  dismiss: () => {
    const { dismissTimer } = get();
    if (dismissTimer) clearTimeout(dismissTimer);
    set({ status: "IDLE", activeTask: null, extraCount: 0, runningTasks: [], dismissTimer: null });
  },
}));
```

- [ ] **Step 3b: Verify TypeScript compiles**

Run: `cd D:/PythonStuff/RetroStation/frontend && npx tsc --noEmit`

Expected: Clean (0 errors).

- [ ] **Step 3c: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add frontend/src/store/progressStore.ts
git commit -m "feat(store): add runningTasks collection and hasRunningType() to progressStore"
```

---

## Task 4: Add `useScanLibrary()` Mutation

**Files:**
- Modify: `frontend/src/api/library.ts`

- [ ] **Step 4a: Add the mutation hook**

Modify `frontend/src/api/library.ts`. Replace the full file with:

```typescript
import { useQuery, useMutation } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { LibraryStatus } from "@/lib/schemas/library";

const LIBRARY_STATUS_KEY = ["library", "status"] as const;

export function useLibraryStatus() {
  return useQuery<LibraryStatus>({
    queryKey: LIBRARY_STATUS_KEY,
    queryFn: () => apiFetch<LibraryStatus>("/api/v1/library/status"),
  });
}

export function useScanLibrary() {
  return useMutation<void, Error, { root_path: string }>({
    mutationFn: (body) =>
      apiFetch<void>("/api/v1/library/scan", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  });
}
```

- [ ] **Step 4b: Verify TypeScript compiles**

Run: `cd D:/PythonStuff/RetroStation/frontend && npx tsc --noEmit`

Expected: Clean (0 errors).

- [ ] **Step 4c: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add frontend/src/api/library.ts
git commit -m "feat(api): add useScanLibrary() mutation hook"
```

---

## Task 5: Create `ScanLibraryButton` Component

**Files:**
- Create: `frontend/src/components/domain/library/ScanLibraryButton.tsx`

- [ ] **Step 5a: Create the component**

Create `frontend/src/components/domain/library/ScanLibraryButton.tsx`:

```tsx
import { RefreshCw } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { useScanLibrary } from "@/api/library";
import { useSettings } from "@/api/settings";
import { useProgressStore } from "@/store/progressStore";

export function ScanLibraryButton() {
  const { data: settings } = useSettings();
  const scanMutation = useScanLibrary();
  const hasRunningScan = useProgressStore((s) => s.hasRunningType("scan"));

  const localPath = settings?.["local_path_prefix"];
  const hasPath = !!localPath;
  const isDisabled = !hasPath || hasRunningScan || scanMutation.isPending;

  function handleClick() {
    if (!localPath) return;
    scanMutation.mutate({ root_path: localPath });
  }

  // Determine tooltip for disabled states
  let tooltip: string | undefined;
  if (!hasPath) {
    tooltip = "Configure your library path in Settings first";
  } else if (hasRunningScan) {
    tooltip = "Scan in progress";
  }

  const button = (
    <button
      type="button"
      onClick={handleClick}
      disabled={isDisabled}
      className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
    >
      {scanMutation.isPending ? (
        <>
          <Spinner className="h-4 w-4" />
          Starting scan...
        </>
      ) : (
        <>
          <RefreshCw className="h-4 w-4" />
          Scan Library
        </>
      )}
    </button>
  );

  // Wrap in span for tooltip on disabled buttons (cross-browser safe)
  if (tooltip) {
    return <span title={tooltip}>{button}</span>;
  }

  return button;
}
```

- [ ] **Step 5b: Verify TypeScript compiles**

Run: `cd D:/PythonStuff/RetroStation/frontend && npx tsc --noEmit`

Expected: Clean (0 errors).

- [ ] **Step 5c: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add frontend/src/components/domain/library/ScanLibraryButton.tsx
git commit -m "feat(ui): create ScanLibraryButton component with 4 states"
```

---

## Task 6: Add `ScanLibraryButton` to Library Page and Path Configuration

**Files:**
- Modify: `frontend/src/pages/library/LibraryStatus.tsx`
- Modify: `frontend/src/pages/settings/PathConfiguration.tsx`

- [ ] **Step 6a: Add button to LibraryStatus page header**

Modify `frontend/src/pages/library/LibraryStatus.tsx`. Replace the `PageHeader` actions prop (lines 44-52):

Replace:
```tsx
        actions={
          <Link
            to="/library/artists"
            className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            <Users className="h-4 w-4" />
            Browse Artists
          </Link>
        }
```

With:
```tsx
        actions={
          <>
            <ScanLibraryButton />
            <Link
              to="/library/artists"
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
            >
              <Users className="h-4 w-4" />
              Browse Artists
            </Link>
          </>
        }
```

Also add the import at the top of the file (after the existing imports):
```tsx
import { ScanLibraryButton } from "@/components/domain/library/ScanLibraryButton";
```

- [ ] **Step 6b: Add button to PathConfiguration footer**

Modify `frontend/src/pages/settings/PathConfiguration.tsx`. Replace the footer `<div>` (lines 159-174):

Replace:
```tsx
          <div className="flex items-center justify-between border-t border-gray-100 bg-gray-50 px-4 py-3">
            {toast ? (
              <InlineToast message={toast} onDismiss={() => setToast(null)} />
            ) : (
              <span />
            )}
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={updateSetting.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
            >
              <Save className="h-4 w-4" />
              {updateSetting.isPending ? "Saving..." : "Save"}
            </button>
          </div>
```

With:
```tsx
          <div className="flex items-center justify-between border-t border-gray-100 bg-gray-50 px-4 py-3">
            <div className="flex items-center gap-3">
              <ScanLibraryButton />
              {toast && (
                <InlineToast message={toast} onDismiss={() => setToast(null)} />
              )}
            </div>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={updateSetting.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-60"
            >
              <Save className="h-4 w-4" />
              {updateSetting.isPending ? "Saving..." : "Save"}
            </button>
          </div>
```

Also add the import at the top of the file (after the existing imports):
```tsx
import { ScanLibraryButton } from "@/components/domain/library/ScanLibraryButton";
```

- [ ] **Step 6c: Verify TypeScript compiles**

Run: `cd D:/PythonStuff/RetroStation/frontend && npx tsc --noEmit`

Expected: Clean (0 errors).

- [ ] **Step 6d: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add frontend/src/pages/library/LibraryStatus.tsx frontend/src/pages/settings/PathConfiguration.tsx
git commit -m "feat(ui): add ScanLibraryButton to Library page and Path Configuration"
```

---

## Task 7: Add Sidebar Progress Indicator

**Files:**
- Modify: `frontend/src/components/layout/Sidebar.tsx`

- [ ] **Step 7a: Add task-to-nav mapping and pulsing dot**

Replace the full contents of `frontend/src/components/layout/Sidebar.tsx` with:

```tsx
import { NavLink } from "react-router-dom";
import { Radio, Library, GitCompare, Settings } from "lucide-react";
import { cn } from "@/lib/utils";
import { useProgressStore } from "@/store/progressStore";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/stations", label: "Stations", icon: Radio },
  { to: "/library", label: "Library", icon: Library },
  { to: "/matcher", label: "Matcher", icon: GitCompare },
  { to: "/settings", label: "Settings", icon: Settings },
];

const TASK_TYPE_TO_NAV: Record<string, string> = {
  scan: "/library",
  enrichment: "/library",
  ingestion: "/stations",
  matching: "/matcher",
  m3u_export: "/stations",
  rules_apply: "/stations",
  // Settings intentionally absent — no tasks route there.
};

export function Sidebar() {
  const runningTasks = useProgressStore((s) => s.runningTasks);

  return (
    <aside className="fixed left-0 top-0 h-full w-64 bg-gray-900 flex flex-col z-40">
      {/* Logo */}
      <div className="px-6 py-5 border-b border-gray-800">
        <span className="text-white text-lg font-bold tracking-wide">
          RetroStation
        </span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => {
          const showDot = runningTasks.some(
            (t) => TASK_TYPE_TO_NAV[t.task_type] === to,
          );

          return (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-gray-800 text-white"
                    : "text-gray-400 hover:bg-gray-800 hover:text-white"
                )
              }
            >
              <Icon className="h-5 w-5 flex-shrink-0" />
              {label}
              {showDot && (
                <span className="ml-auto h-2 w-2 rounded-full bg-blue-500 animate-pulse" />
              )}
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}
```

- [ ] **Step 7b: Verify TypeScript compiles**

Run: `cd D:/PythonStuff/RetroStation/frontend && npx tsc --noEmit`

Expected: Clean (0 errors).

- [ ] **Step 7c: Commit**

```bash
cd D:/PythonStuff/RetroStation
git add frontend/src/components/layout/Sidebar.tsx
git commit -m "feat(ui): add pulsing activity dot to Sidebar nav items for running tasks"
```

---

## Task 8: Full Verification

- [ ] **Step 8a: Run all backend tests**

Run: `cd D:/PythonStuff/RetroStation && python -m pytest tests/ -v --tb=short`

Expected: All tests PASS.

- [ ] **Step 8b: Run mypy on all changed backend files**

Run: `cd D:/PythonStuff/RetroStation && python -m mypy backend/services/library_scan_service.py backend/tasks/library_tasks.py --strict`

Expected: Clean (0 errors).

- [ ] **Step 8c: Run ruff**

Run: `cd D:/PythonStuff/RetroStation && python -m ruff check backend/services/library_scan_service.py backend/tasks/library_tasks.py`

Expected: Clean.

- [ ] **Step 8d: Run frontend type check**

Run: `cd D:/PythonStuff/RetroStation/frontend && npx tsc --noEmit`

Expected: Clean (0 errors).

- [ ] **Step 8e: Visual smoke test**

Run: `cd D:/PythonStuff/RetroStation/frontend && npm run dev`

Open: `http://localhost:5173`

Verify:
1. **Library page** (`/library`): "Scan Library" button visible next to "Browse Artists"
2. **Settings > Path Configuration** (`/settings/paths`): "Scan Library" button in footer row, left of Save
3. **If `local_path_prefix` is empty**: button is disabled with tooltip "Configure your library path in Settings first"
4. **If `local_path_prefix` is set**: button is enabled; clicking triggers scan
5. **During scan**: bottom ProgressBar shows "Scanning library" with percentage; sidebar shows pulsing blue dot next to "Library"
6. **After scan**: ProgressBar shows green checkmark "Done", dot disappears
