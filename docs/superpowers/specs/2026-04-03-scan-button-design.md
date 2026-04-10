# Scan Library Button + Progress Tracking + Sidebar Indicator

**Date:** 2026-04-03
**Status:** Approved

## Overview

Add a "Scan Library" button to the Library page and Path Configuration page that triggers a scan of the user's configured `local_path_prefix`. Wire the existing scan task to the progress tracking infrastructure so the bottom ProgressBar shows real-time file counts. Add a pulsing activity dot to the Sidebar nav items when background tasks are running.

## Scope

### In scope
- Backend: wire `library_scan_task` to `progress_tracking` table with real-time updates
- Frontend: `ScanLibraryButton` component on Library page and Path Configuration page
- Frontend: `progressStore` gains `runningTasks` collection and `hasRunningType()` getter
- Frontend: Sidebar pulsing dot indicator for running tasks
- New `useScanLibrary()` mutation in `library.ts`

### Out of scope
- Automatic/scheduled scanning (no file watchers, no startup triggers)
- Changes to `ScannerActions.tsx` at `/matcher/scanner` — **intentionally left as-is** as a power-user escape hatch for scanning arbitrary paths outside `local_path_prefix`
- Library status query invalidation after scan completes (deferred follow-up)

## Section 1: Backend — Wire Scan Task to Progress Tracking

### Files changed
- `backend/services/library_scan_service.py` — add `on_progress` callback parameter
- `backend/tasks/library_tasks.py` — add progress tracking writes

### `scan_directory()` signature change

```python
def scan_directory(
    root: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[LibraryFile], list[LibraryQuarantine]]:
```

### Traversal with pre-count

1. **Pre-count pass:** `candidates = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)`
2. `total = len(candidates)`
3. **Iterate `candidates`:** For each file, extract tags. Increment `processed`. Fire `on_progress(processed, total, str(path))` every 50 files and on the final file.
4. Return `(files, quarantine)` as before — bulk-return pattern preserved.

### `library_scan_task()` changes

```python
@huey.task()
def library_scan_task(root_path: str) -> str:
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    last_progress: dict = {"processed": 0, "total": 0, "current_path": ""}

    # Separate autocommit connection for progress tracking.
    # Intentional layer skip past RepositoryFactory — progress writes must be
    # visible immediately (not held in the library data transaction).
    progress_conn = None
    progress_repo: PgTaskProgressRepository | None = None
    try:
        progress_conn = psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)
        progress_repo = PgTaskProgressRepository(progress_conn)

        # Initial record
        progress_repo.upsert(TaskProgress(
            task_id=task_id,
            task_type=TaskType.SCAN,
            status=TaskStatus.RUNNING,
            progress_data=last_progress,
            started_at=task_started_at,
            updated_at=task_started_at,
        ))

        # Callback — closes over task_id, task_started_at, last_progress, progress_repo
        def on_progress(processed: int, total: int, current_path: str) -> None:
            nonlocal last_progress
            last_progress = {"processed": processed, "total": total, "current_path": current_path}
            progress_repo.upsert(TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
            ))

        files, quarantine = scan_directory(Path(root_path), on_progress=on_progress)

        # Persist library data — main transactional connection
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            repos = RepositoryFactory(conn)
            for lf in files:
                repos.library_files.upsert(lf)
            for q in quarantine:
                repos.library_quarantine.create(q)
            conn.commit()

        # Mark completed AFTER library data commit succeeds
        progress_repo.upsert(TaskProgress(
            task_id=task_id,
            task_type=TaskType.SCAN,
            status=TaskStatus.COMPLETED,
            progress_data=last_progress,
            started_at=task_started_at,
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        ))

    except Exception as exc:
        if progress_conn is not None and progress_repo is not None:
            try:
                progress_repo.upsert(TaskProgress(
                    task_id=task_id,
                    task_type=TaskType.SCAN,
                    status=TaskStatus.FAILED,
                    progress_data={**last_progress, "error": str(exc)},
                    started_at=task_started_at,
                    updated_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                ))
            except Exception:
                pass  # Don't mask the original error
        raise

    finally:
        if progress_conn is not None:
            progress_conn.close()
```

### Key decisions
- **`task_id`:** Fresh `uuid.uuid4().hex` — no Huey dependency.
- **`updated_at`:** Caller supplies `datetime.now(UTC)` on every `upsert()`. No model default change.
- **Callback throttle:** Every 50 files. At 500ms WebSocket poll interval, this avoids DB churn without visible lag.
- **Completion point:** After `conn.commit()` on library data, not after the walk.
- **`progress_data` keys:** `processed` and `total` (matching `ProgressBar.tsx`'s `getPercent()` expectations), plus `current_path`.

## Section 2: Frontend — Scan Library Buttons

### Files changed
- `frontend/src/api/library.ts` — add `useScanLibrary()` mutation
- `frontend/src/components/domain/library/ScanLibraryButton.tsx` — new shared component
- `frontend/src/pages/library/LibraryStatus.tsx` — add button to header
- `frontend/src/pages/settings/PathConfiguration.tsx` — add button to footer row
- `frontend/src/store/progressStore.ts` — add `runningTasks` and `hasRunningType()`

### `useScanLibrary()` mutation

New export in `library.ts`:

```ts
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

### `progressStore` changes

Add to the Zustand store:

- `runningTasks: TaskInfo[]` — full list of tasks with `status === "running"`, updated from WebSocket feed
- `hasRunningType(type: string): boolean` — `runningTasks.some(t => t.task_type === type)`

The existing `activeTask` / `extraCount` / `status` / `dismiss` API stays unchanged. `runningTasks` is the backing data that `activeTask` is derived from.

### `ScanLibraryButton` component

**Props:** None (self-contained — reads all state from hooks).

**State logic:**

| Condition | Button state |
|-----------|-------------|
| `local_path_prefix` not set (empty/null from `useSettings()`) | Disabled, wrapped in `<span title="Configure your library path in Settings first">` |
| `hasRunningType("scan")` is true | Disabled, wrapped in `<span title="Scan in progress">` |
| `useScanLibrary().isPending` is true | Disabled, shows spinner + "Starting scan..." |
| Otherwise | Enabled, shows "Scan Library" with refresh icon |

**On click:** calls `mutate({ root_path: local_path_prefix })`.

**Important:** reads `local_path_prefix` from `useSettings()` (the persisted value, not any form draft). On the PathConfiguration page, after Save completes, the settings query is invalidated and the button re-evaluates automatically via the same query key.

### Scan-running gate

Gates on **any** running scan task globally (not session-scoped). Single-user app; the `progress_tracking` table is the single source of truth regardless of how the scan was triggered.

### Library page placement

Button in the page header area, alongside existing heading/controls.

### Path Configuration page placement

Same footer row as the Save button. The left side becomes `flex items-center gap-3` holding `ScanLibraryButton` and the existing `InlineToast` (when visible). `justify-between` is preserved, Save stays on the right:

```
[ ScanLibraryButton  (toast?)          Save ]
```

### Backend validation note

`library_scan_paths` config defaults to `[]` (no restrictions), so sending `local_path_prefix` as `root_path` passes validation. If the user later populates the allowlist, they must include their `local_path_prefix` — reasonable for a single-user app.

## Section 3: Sidebar Progress Indicator

### Files changed
- `frontend/src/components/layout/Sidebar.tsx` — add activity dot

### Design

A pulsing blue dot (8px) appears trailing (`ml-auto`) in the nav item row when any running task maps to that nav section:

```
[ icon  Library            • ]
```

### Task-to-nav mapping

```ts
const TASK_TYPE_TO_NAV: Record<string, string> = {
  scan: "/library",
  enrichment: "/library",
  ingestion: "/stations",
  matching: "/matcher",
  m3u_export: "/stations",
  rules_apply: "/stations",
  // Settings intentionally absent — no tasks route there.
};
```

### Per-item logic

```ts
const showDot = runningTasks.some(t => TASK_TYPE_TO_NAV[t.task_type] === to);
```

If `showDot` is true, render:

```tsx
<span className="ml-auto h-2 w-2 rounded-full bg-blue-500 animate-pulse" />
```

### Dependency

Blocked on Section 2's `progressStore` change (`runningTasks` collection). Cannot be implemented before that.

### Why not a mini progress bar?

A percentage bar in a 256px nav column creates layout complexity (text truncation, responsive sizing) for minimal added value. The bottom ProgressBar shows detail; the sidebar dot answers "is something happening?"

## Testing Strategy

### Backend
- Unit test: `scan_directory()` with `on_progress` callback — verify it fires every 50 files and on the last file, with correct `(processed, total, current_path)` values
- Unit test: `library_scan_task()` — mock `scan_directory` and verify progress tracking records are created (RUNNING), updated, and completed (COMPLETED after commit)
- Unit test: failure path — verify FAILED status is written with error message when an exception occurs
- Integration test: scan a small test directory and verify `progress_tracking` rows are visible via a second connection during the scan

### Frontend
- `ScanLibraryButton`: test all four states (no path, scan running, loading, enabled)
- `progressStore`: test `hasRunningType()` with various task combinations
- Sidebar: test dot appears/disappears based on `runningTasks` state

## Follow-ups (out of scope)
- Invalidate `library/status` query when scan completes (stale counts after scan)
- Consider adding `local_path_prefix` to the `library_scan_paths` allowlist check automatically
