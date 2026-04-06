# Lessons Learned

## 2026-04-03: Worker process had no logging output

**Pattern:** Multi-process architecture (FastAPI + Huey worker) where logging config was only called in the API server's lifespan. The worker process never initialized structlog, so all log calls were silently dropped.

**Rule:** When adding a new process entry point (worker, CLI, migration script), always verify that logging is configured at startup. Each process needs its own `configure_logging()` call — it doesn't inherit from the API server.

**Fix:** Called `configure_logging()` in `backend/tasks/huey_app.py` so it runs when the Huey consumer imports the module.

---

## 2026-04-03: Null bytes in ID3 tags crash PostgreSQL inserts

**Pattern:** Mutagen extracts raw ID3 tag values that can contain embedded null bytes (`\x00`). These survive `str()` conversion and reach PostgreSQL via `json.dumps()` in the repository layer. PostgreSQL rejects `\u0000` in `text`/`jsonb` columns with `UntranslatableCharacter`.

**Rule:** Sanitise external data (file metadata, user input, API responses) at the boundary where it enters the domain — not in the database layer. The extraction function that reads tags is the right place, because it covers all fields uniformly and keeps the repository layer clean.

**Fix:** Added `_sanitise_tag_value()` in `library_scan_service.py` that strips `\x00` from all tag string values before they enter the `LibraryFile` model.

---

## 2026-04-04: Adding abstract methods breaks fakes (ABC compliance)

**Pattern:** When adding a new `@abstractmethod` to an ABC (`LibraryFileRepository`, `LibraryQuarantineRepository`), the corresponding test fakes become non-instantiable. The ABC compliance tests in `test_fakes_implement_abcs.py` catch this immediately, but the initial plan missed the fakes entirely.

**Rule:** Any plan that adds an abstract method to a repository interface MUST include a step to update the fake in `tests/fakes/`. Check `test_fakes_implement_abcs.py` — if it tests the fake, the fake must implement the new method. Always include fakes in the file map.

**Fix:** Added `upsert_write_only` and `create_write_only` to both fakes, delegating to the existing methods.

---

## 2026-04-04: Extracting functions disconnects shared mutable state

**Pattern:** When `library_scan_task` was refactored to extract `_run_scan()`, the `last_progress` dict ended up as two separate variables — one in the Huey task (used for COMPLETED/FAILED records) and one in `_run_scan` (updated by the `on_progress` callback). The Huey task's copy was never updated, so COMPLETED/FAILED progress records always showed `{"processed": 0, "total": 0}`.

**Rule:** When extracting a function from a larger scope, audit every mutable variable that was previously shared via closure. If the extracted function updates a value that the caller later reads, the extracted function must return it (or accept a mutable container). Don't assume the caller's copy stays in sync.

**Fix:** Changed `_run_scan` to return `(files_written, quarantine_written, last_progress)` as a 3-tuple so the caller gets the final state.

---

## 2026-04-04: Parallel test subprocesses caused memory exhaustion

**Pattern:** Running tests across multiple parallel subagents (or multiple concurrent Bash calls) each spawned separate Python/pytest processes. These accumulated to 80+ orphaned Python processes consuming 81% of system memory, requiring manual `taskkill /F /IM python.exe`.

**Rule:** Always run the full test suite in a SINGLE `pytest` invocation. Never split tests across parallel subagents or concurrent Bash commands. One pytest command, one process tree, clean shutdown.

**Fix:** Use `pytest` once with appropriate flags (e.g., `-x` for fail-fast, specific paths for targeted runs). Chain sequential test runs in a single Bash call if needed.
