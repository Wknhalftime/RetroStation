# Auto-Enrichment Watcher Design

**Date:** 2026-04-05
**Status:** Draft (rev 2 — addressing review feedback)
**Approach:** Periodic polling with folder-hash diffing (no external dependencies)

## Problem

The library enrichment pipeline (scan -> enrich -> MB enhance) requires manual triggering. New audio files added to the library directory are not automatically detected or enriched. Additionally, rescanning overwrites enrichment status for unchanged files, causing unnecessary re-enrichment.

## Goals

1. Automatically detect new/modified/missing audio files in the configured library directory
2. Trigger targeted scans only for changed folders, skipping unchanged subtrees
3. Chain scan completion into enrichment automatically
4. Never re-enrich unchanged files
5. Surface missing files to the user for decision (not auto-delete)

## Non-Goals

- Frontend UI for missing file management (follow-up work)
- Multiple root directory support (single root is sufficient)
- File-system watcher library (using periodic polling instead)

## Architecture

### Settings: Watch Path Source

**Two config mechanisms exist in the codebase:**
- `library_scan_paths: list[str]` in `backend/config.py` — env-based allowlist, used by `POST /scan` to validate requested paths
- `local_path_prefix` in `user_settings` DB table — GUI-configured path, used for M3U path mapping

**Decision:** The watcher reads `local_path_prefix` from the `user_settings` table via `SettingsRepository.get("local_path_prefix")`. This is the path the user configured in the GUI (Settings > Path Configuration). If the value is empty or unset, the watcher is a no-op.

The watcher does NOT need to validate against `library_scan_paths` because `local_path_prefix` is user-configured through the GUI — the user has already implicitly authorized watching that path.

**Existing infrastructure (no new files needed):** `user_settings` table is defined in migration `0006_settings_and_ops.sql`. `PgSettingsRepository` exists at `backend/db/repositories/settings.py` and is already registered in `RepositoryFactory` as `repos.settings`.

### Trigger Flow

```
Every 4 min: library_watcher_poll (@huey.periodic_task(crontab(minute='*/4')))
    |
    v
Read local_path_prefix from user_settings via SettingsRepository (empty? -> no-op)
    |
    v
folder_hash_service.diff_tree(): walk tree, compute (mtime, size) hashes in memory, diff against DB
    |
    v
Coalesce: merge child paths into changed parents
    |
    v
Changed folders found? --no--> return (no-op)
    |
    yes
    v
Stage pending_hashes to library_folder_staged_hashes table
    |
    v
library_scan_files_task(coalesced_paths) [fire-and-forget]
    |
    v
Acquire PG advisory lock: pg_try_advisory_lock(hashtext(root_path)::bigint)
    (held? -> log warning, exit)
    |
    v
Smart per-folder scan (diff disk vs DB: skip/update/insert/mark missing/quarantine)
    |
    v
Persist folder hashes from staged table (only on success), clear staged rows
    |
    v
library_enrichment_task() (fire-and-forget chain) — NEW addition to scan path
    |
    v
mb_enrichment_task() (existing chain)
```

**Note on `crontab(minute='*/4')`:** This fires on the clock (at :00, :04, :08, etc.), not "4 minutes after the previous run." If a poll takes longer than 4 minutes, the next invocation queues behind it. The advisory lock ensures the queued scan exits gracefully if one is already running.

### Folder Hash Tree

A new `library_folders` table stores a Merkle-tree-like structure of the library directory:

| Column | Type | Purpose |
|--------|------|---------|
| `id` | UUID | Primary key |
| `parent_id` | UUID (nullable) | FK to self, null for root |
| `name` | TEXT | Directory name (not full path) |
| `full_path` | TEXT (unique) | Full path for lookups |
| `folder_hash` | TEXT | Hash derived from children |

**Hash computation (bottom-up) — uses mtime + size, not SHA-256:**

File-level hash input: `f"{file_mtime}:{file_size}"` for each audio file in the folder (via `os.stat()`). This is orders of magnitude faster than reading file contents — a stat call vs. full file read. Sufficient for detecting changes on a local filesystem (single-user tool). SHA-256 content hashing remains only in `library_files.file_hash` for deduplication at the DB level.

- Leaf folder hash = `sha256(sorted(f"{filename}:{mtime}:{size}" for each audio file))`
- Parent folder hash = `sha256(sorted(child_folder_hashes) + sorted(file_stat_hashes))`
- If a folder's stored hash matches computed hash, skip the entire subtree

### Smart Scanner: Per-Folder Diffing

When the scanner enters a changed folder, it loads existing DB records for that folder path (including `MISSING` status files) and compares against files on disk:

| Scenario | Disk State | DB State | Action |
|----------|------------|----------|--------|
| Unchanged file | Present, hash matches | Exists, same hash | No DB write |
| Modified file | Present, hash differs | Exists, different hash | Update row, reset `enrichment_status` to `PENDING`, set `file_status=PRESENT` |
| New file | Present | No matching path | Insert with `enrichment_status=PENDING`, `file_status=PRESENT` |
| Re-appeared file | Present | Exists with `file_status=MISSING` | Set `file_status=PRESENT`, keep existing `enrichment_status` (do NOT reset to PENDING unless hash changed) |
| Missing file | Not on disk | Exists with `file_status=PRESENT` | Set `file_status=MISSING`, preserve `enrichment_status` |
| Parse failure | Present, Mutagen error | Any | Create/update `LibraryQuarantine` entry (same as full scan), do not modify `library_files` row |

**Re-appeared file handling:** When a file's path matches an existing `MISSING` row, the scanner compares the file hash. If unchanged: restore `file_status=PRESENT`, keep enrichment intact. If hash changed: restore `file_status=PRESENT` and reset `enrichment_status=PENDING`.

**Quarantine handling:** Targeted scans use the same quarantine logic as full scans. If a previously-quarantined file now parses successfully, the quarantine entry is removed and a normal `library_files` row is created.

### File Status (New Field)

A new `file_status` field on `library_files`, orthogonal to `enrichment_status`:

| Value | Meaning |
|-------|---------|
| `PRESENT` | File exists on disk (default) |
| `MISSING` | File was in DB but no longer found on disk |
| `DELETED` | User confirmed deletion of missing file |

State transitions:
- `PRESENT -> MISSING`: file not found on disk during scan
- `MISSING -> PRESENT`: file re-appears on disk (network drive remount, file restored)
- `MISSING -> DELETED`: user confirms deletion via GUI (follow-up work)
- `DELETED`: terminal state, row may be cleaned up

### Upsert Behavior Change

The current `upsert()` and `upsert_write_only()` in `backend/db/repositories/library_files.py` overwrite all columns on conflict, including `enrichment_status`. This breaks Goal #4 (never re-enrich unchanged files).

**New conflict resolution logic:**

```sql
INSERT INTO library_files (id, file_path, file_hash, format, enrichment_status, ...)
VALUES (...)
ON CONFLICT (file_path) DO UPDATE SET
    file_hash         = EXCLUDED.file_hash,
    format            = EXCLUDED.format,
    -- Only reset enrichment if the file content actually changed
    enrichment_status = CASE
        WHEN library_files.file_hash = EXCLUDED.file_hash
        THEN library_files.enrichment_status   -- unchanged: keep current status
        ELSE EXCLUDED.enrichment_status        -- changed: reset to PENDING
    END,
    file_status       = 'PRESENT',
    -- Always update metadata fields (tags may have changed even if audio hasn't)
    recording_mbid    = EXCLUDED.recording_mbid,
    artist_mbid       = EXCLUDED.artist_mbid,
    ...
    updated_at        = NOW()
```

This is used by both the full scan and the targeted scan. The smart scanner's "unchanged file" case (hash match) skips the upsert entirely — this SQL is the safety net for when a file IS written.

### Post-Scan Enrichment Chaining

**This is a fix to a pre-existing gap.** The current `library_scan_task` (line 199 of `library_tasks.py`) returns `root_path` without chaining to enrichment. The `enrichment -> mb_enrichment` chain exists, but `scan -> enrichment` does not.

**Change:** Both `library_scan_task` (full scan) and `library_scan_files_task` (targeted scan) will fire `library_enrichment_task()` on completion if `files_written > 0`. Fire-and-forget, consistent with existing chaining pattern.

**Regression risk:** Users who currently trigger scans without wanting automatic enrichment will now get enrichment. This is the desired behavior per the design goals, but worth noting. The enrichment task is idempotent (processes only PENDING files), so running it unnecessarily is harmless.

### Periodic Poll Task

**Task:** `library_watcher_poll`
**Decorator:** `@huey.periodic_task(crontab(minute='*/4'))`
**Behavior:**

1. Read `local_path_prefix` from `user_settings` via `SettingsRepository.get("local_path_prefix")` — if empty, return
2. Call `folder_hash_service.diff_tree(root_path)` to get changed folder paths + computed hashes (in memory only)
3. Coalesce paths: merge children into parent where parent is also changed
4. If no changes, return
5. Stage pending hashes to `library_folder_staged_hashes` table (avoids large serialized payloads in `huey.db`)
6. Enqueue `library_scan_files_task(coalesced_paths)` — fire-and-forget
7. Folder hashes are persisted only after the scan task completes successfully (reads from staged table, writes to `library_folders`, clears staged rows)

### First-Run Initialization

On the first poll (no rows in `library_folders`):

1. Walk the directory tree under `local_path_prefix`
2. For each directory, create a `library_folders` row with `parent_id` linking to its parent
3. For each audio file, compute `f"{filename}:{mtime}:{size}"` stat hash
4. Query `library_files` for all rows whose `file_path` starts with the folder's `full_path` — these are already-scanned files
5. Build folder hashes bottom-up from file stat hashes
6. Write all folder hashes to `library_folders`
7. Do NOT trigger a scan — the current DB state is assumed correct for existing files
8. On the next poll (4 minutes later), the diff will detect any files added between the initial scan and now

This means the first poll is a pure setup operation. Subsequent polls are lightweight diffs.

### Progress Tracking for Targeted Scans

The existing `library_scan_task` reports progress via `PgProgressTrackingRepository` (drives the scan progress UI). The new `library_scan_files_task` will use the same mechanism:

- Creates a `ProgressTracking` record with `task_type=TaskType.SCAN`
- Reports `{processed, total, current_path}` as it processes folders
- The frontend progress UI works unchanged — it queries by `task_type`, not by which task created the record

**Distinction:** Watcher-triggered scans will appear in the same progress UI as manual scans. The `progress_data` will include a `"source": "watcher"` field so the frontend can differentiate if needed in a future UI update.

## Robustness Patterns

### When to Persist Folder Hashes

Folder hashes must NOT be updated until the targeted scan completes successfully. If hashes are persisted after diff but before the scan finishes, a crash or failure mid-scan would leave the tree looking "clean" on the next poll, silently skipping unprocessed files.

**Strategy:** The poll task stages computed hashes in a `library_folder_staged_hashes` table (folder_id, new_hash). The scan task reads from this table on success, batch-updates `library_folders.folder_hash`, and clears the staged rows. On failure, the staged rows remain and will be overwritten on the next poll.

### Staged Hashes Table

Avoids two problems: (1) large serialized payloads in `huey.db` for libraries with many changed folders, and (2) loss of computed hashes if the worker crashes between enqueue and task execution.

```sql
CREATE TABLE library_folder_staged_hashes (
    folder_id       UUID NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
    new_hash        TEXT NOT NULL,
    staged_by_task  TEXT NOT NULL,
    staged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (folder_id, staged_by_task)
);
```

The `staged_by_task` column (Huey task ID) ensures each scan task only commits hashes it staged. If Poll #1 stages H1 for Task A and Poll #2 stages H2 for Task B before Task A runs, Task A commits only rows matching its task ID, leaving Task B's rows intact for when it executes.

### Coalescing Changed Paths

Before enqueueing a scan job, merge changed folder paths so a parent subsumes its children. If both `/music/jazz` and `/music/jazz/miles` are flagged as changed, only `/music/jazz` is passed to the scanner (which walks recursively). This avoids scanning the same files twice.

### Concurrency: PostgreSQL Advisory Lock

With SqliteHuey `-w 1`, only one Huey task runs at a time — so overlapping poll+scan races are impossible within the consumer. However, a manual `POST /scan` from the FastAPI API server could overlap with a watcher-triggered scan (the API calls `library_scan_task()` which enqueues into Huey, but the scan logic opens its own PG connection).

**Strategy:** Acquire a PostgreSQL advisory lock at the start of any scan task:

```python
# Key derivation: hashtext() returns int4, cast to bigint for pg_try_advisory_lock
lock_acquired = conn.execute(
    "SELECT pg_try_advisory_lock(hashtext(%s)::bigint)",
    (root_path,)
).fetchone()[0]
if not lock_acquired:
    logger.warning("scan_lock_held", root=root_path)
    return
```

The lock is session-scoped (released when the PG connection closes), so it auto-releases even on crash. This is a **PostgreSQL** lock, not a SQLite lock — it protects the data DB, not `huey.db`.

### Huey Task Queue Considerations

The project uses `SqliteHuey` with `-w 1` (single worker). Key constraints:

- **Never call `.get()` on a task result from within a task** — deadlocks the single worker. All chaining is fire-and-forget.
- With one worker, tasks naturally serialize. Queue depth may grow if scans are slow, but this is acceptable for a single-user tool.
- If a future multi-worker move happens (RedisHuey), the MusicBrainz rate limiter would need a cross-process throttle (current thread-lock is per-process only).

### Huey Result Store Accumulation

`SqliteHuey` is initialized with `results=True`. Every completed task writes its return value to `huey.db`. Since watcher tasks are fire-and-forget (nobody calls `.get()` to consume results), result rows accumulate indefinitely. With a 4-minute poll cycle, this adds ~360 result rows/day.

**Mitigation:** The new watcher and scan tasks should return `None` to minimize stored result size. At ~360 rows/day, `huey.db` stays small for years in a single-user tool, so this is a low-priority concern. If it ever matters, switching to `SqliteHuey(filename="huey.db", results=False)` globally is the cleanest fix (Huey 2.x does not support per-task result disabling).

### Path Canonicalization

`full_path` in `library_folders` and `file_path` in `library_files` must use consistent canonicalization to ensure UNIQUE constraints and equality checks work reliably:
- Normalize to forward slashes (or OS-native, applied consistently)
- Apply Unicode NFC normalization
- On Windows: lowercase drive letter, consistent casing policy
- `os.path.normpath()` + NFC as the canonical form

### Schema Index Notes

- `idx_library_files_status` uses `WHERE file_status != 'PRESENT'` — optimal since most files are PRESENT and queries typically filter for MISSING.
- For UI queries showing only MISSING files, a tighter `WHERE file_status = 'MISSING'` partial index may be more efficient. Can be added when the missing-files UI is built.
- Optional future enhancement: `folder_id` FK on `library_files` to avoid path-prefix joins. Not required for v1.

## Schema Changes

### New Table: `library_folders`

```sql
CREATE TABLE library_folders (
    id          UUID PRIMARY KEY,
    parent_id   UUID REFERENCES library_folders(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    full_path   TEXT NOT NULL UNIQUE,
    folder_hash TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_library_folders_parent ON library_folders(parent_id);
```

### New Table: `library_folder_staged_hashes`

```sql
CREATE TABLE library_folder_staged_hashes (
    folder_id       UUID NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
    new_hash        TEXT NOT NULL,
    staged_by_task  TEXT NOT NULL,
    staged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (folder_id, staged_by_task)
);
```

### Alter: `library_files`

```sql
CREATE TYPE file_status AS ENUM ('PRESENT', 'MISSING', 'DELETED');
ALTER TABLE library_files ADD COLUMN file_status file_status NOT NULL DEFAULT 'PRESENT';
CREATE INDEX idx_library_files_status ON library_files(file_status) WHERE file_status != 'PRESENT';
```

### Alter: `library_files` upsert (code change, not DDL)

The `ON CONFLICT` clause in `upsert()` and `upsert_write_only()` changes to preserve `enrichment_status` when `file_hash` is unchanged (see §Upsert Behavior Change above for full SQL).

## File Changes

| File | Change |
|------|--------|
| `backend/db/migrations/NNNN_library_folders.sql` | New `library_folders` table, `library_folder_staged_hashes` table, `file_status` column on `library_files` |
| `backend/domain/models.py` | `LibraryFolder` dataclass, `file_status` field on `LibraryFile` |
| `backend/domain/enums.py` | `FileStatus` enum |
| `backend/repositories/library_folders.py` | Abstract repo interface |
| `backend/db/repositories/library_folders.py` | PG implementation (upsert, get_by_path, get_children, update_hash, stage/commit hashes) |
| `backend/services/repository_factory.py` | Register folder repo |
| `backend/services/folder_hash_service.py` | Walk tree, compute mtime+size hashes, diff against stored, coalesce paths |
| `backend/services/library_scan_service.py` | Smart per-folder diffing (all 6 scenarios including re-appeared + quarantine) |
| `backend/tasks/library_watcher_tasks.py` | Periodic poll task (4 min, `@huey.periodic_task(crontab(minute='*/4'))`) |
| `backend/tasks/library_tasks.py` | New `library_scan_files_task` + **fix existing `library_scan_task` to chain into enrichment** (pre-existing gap) |
| `backend/tasks/huey_app.py` | Import new watcher task module |
| `backend/db/repositories/library_files.py` | Upsert conflict clause: preserve `enrichment_status` when hash unchanged, add `file_status` updates, query methods for files by folder path including MISSING |
| Tests | Folder hash diffing, smart scan all 6 scenarios (incl. re-appeared + quarantine), watcher poll, scan->enrich chain, first-run init, advisory lock contention, upsert idempotency |

## Testing Strategy

- **Unit tests:** Folder hash computation (mtime+size), per-folder diff logic (all 6 scenarios), path coalescing, path canonicalization
- **Integration tests:** Full watcher poll cycle with real DB, scan->enrich chain verification, advisory lock contention (two concurrent scans), upsert behavior (unchanged hash preserves enrichment_status, changed hash resets it)
- **Edge cases:** Empty directories, deeply nested trees, first-run initialization, missing `local_path_prefix`, MISSING->PRESENT re-appearance, quarantine->success recovery, large folder count (staged hashes table)
