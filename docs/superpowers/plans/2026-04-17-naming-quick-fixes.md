# Naming & Quick Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five low-risk code quality issues — an event-loop blocking sync call, three parameter naming errors in repository ABCs, a builtin name shadow, an out-of-place domain helper, and two inconsistently named task modules.

**Architecture:** All changes are pure refactors or safe additions. No interfaces change shape; callers use positional arguments so parameter renames are non-breaking. Each task is independently committable with a green test suite.

**Tech Stack:** Python 3.12, pytest 8+, pytest-asyncio (asyncio_mode=auto), psycopg 3, FastAPI, Huey, uv

---

## File Map

| File | Change |
|---|---|
| `backend/routers/tasks.py` | Wrap sync block in `asyncio.to_thread` |
| `backend/repositories/artists.py` | Rename `mbid` → `artist_id` on `get_by_id`, `mark_enhanced`, `mark_enhancement_failed` |
| `backend/db/repositories/artists.py` | Same renames in concrete impl |
| `tests/fakes/artists.py` | Same renames in fake |
| `backend/repositories/library_files.py` | Rename `id` → `file_id` on `get_by_id` and `update_recording_link` |
| `backend/db/repositories/library_files.py` | Same renames in concrete impl |
| `tests/fakes/library_files.py` | Same renames in fake |
| `backend/domain/synthetic_work_id.py` | **Create** — encode/decode helpers moved from router |
| `tests/domain/test_synthetic_work_id.py` | **Create** — unit tests for encode/decode |
| `backend/routers/library.py` | Replace local encode/decode with import from domain |
| `backend/tasks/normalize_backfill_task.py` | **Rename** → `normalize_backfill_tasks.py` |
| `backend/tasks/library_tasks.py` | **Rename** → `library_scan_tasks.py` |
| `backend/tasks/huey_app.py` | Update both renamed imports |
| `backend/routers/library.py` | Update `library_tasks` import |
| `tests/routers/test_tasks.py` | Add `retry_enrichment` test |
| `tests/services/test_scan_progress.py` | Update renamed import |
| `tests/tasks/test_scan_enrichment_chain.py` | Update renamed import |
| `tests/tasks/test_library_tasks.py` | Update renamed import |

---

## Task 1: Wrap retry_enrichment's sync DB call in asyncio.to_thread

**Files:**
- Modify: `backend/routers/tasks.py`
- Test: `tests/routers/test_tasks.py`

- [ ] **Step 1: Write a failing test for retry_enrichment**

Add this test class to `tests/routers/test_tasks.py` (after the existing `TestActiveTasks` class):

```python
class TestRetryEnrichment:
    def test_returns_reset_count_and_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /retry-enrichment resets failed files and returns the count."""
        import pytest
        from unittest.mock import MagicMock, patch

        mock_repo = MagicMock()
        mock_repo.reset_failed_enrichments.return_value = 3

        with (
            patch("backend.routers.tasks.connect_sync") as mock_connect,
            patch("backend.routers.tasks.PgLibraryFileRepository", return_value=mock_repo),
            patch("backend.routers.tasks.asyncio") as mock_asyncio,
        ):
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            mock_asyncio.to_thread = asyncio.to_thread  # let it run for real

            resp = client.post("/api/v1/tasks/retry-enrichment")

        assert resp.status_code == 200
        data = resp.json()
        assert data["reset"] == 3
        assert "3" in data["message"]
```

Actually a cleaner approach — patch only at the task level. Replace the above with:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


class TestRetryEnrichment:
    def test_resets_failed_files_and_returns_count(
        self, client: TestClient
    ) -> None:
        """retry_enrichment endpoint resets failed files and returns count."""
        mock_repo = MagicMock()
        mock_repo.reset_failed_enrichments.return_value = 5

        with (
            patch("backend.routers.tasks.connect_sync") as mock_conn_ctx,
            patch(
                "backend.routers.tasks.PgLibraryFileRepository",
                return_value=mock_repo,
            ),
            patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task"
            ),
        ):
            conn_mock = MagicMock()
            mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=conn_mock)
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.post("/api/v1/tasks/retry-enrichment")

        assert resp.status_code == 200
        data = resp.json()
        assert data["reset"] == 5
        assert "5" in data["message"]
        mock_repo.reset_failed_enrichments.assert_called_once()
```

Also add `import asyncio` and `import pytest` at the top of `tests/routers/test_tasks.py` alongside the existing imports.

- [ ] **Step 2: Run the test to verify it fails**

```
uv run pytest tests/routers/test_tasks.py::TestRetryEnrichment -v
```

Expected: Either `FAILED` or `ERROR` (endpoint exists but no `asyncio` patch needed yet — test may pass already if mocking is sufficient; that's fine, proceed to the implementation change).

- [ ] **Step 3: Refactor retry_enrichment to use asyncio.to_thread**

In `backend/routers/tasks.py`, add `import asyncio` to the imports block (after `import json`), then replace the `retry_enrichment` body:

```python
import asyncio
import json
from datetime import datetime
from typing import Annotated, Any
```

Replace the function body (lines 98–129):

```python
@router.post("/retry-enrichment", response_model=RetryEnrichmentResult)
async def retry_enrichment(_token: Token) -> RetryEnrichmentResult:
    """Reset all enrichment_failed library files to pending and re-enqueue enrichment.

    Resets every ``library_files`` row whose ``enrichment_status`` is ``'failed'``
    back to ``'pending'``, then enqueues a fresh ``library_enrichment_task`` run.
    Use this when enrichment has stalled due to transient MusicBrainz errors or a
    task-level crash.

    Args:
        _token: Bearer token (auth check only).

    Returns:
        :class:`RetryEnrichmentResult` with the count of rows reset and a status
        message.
    """
    from backend.tasks.library_enrichment_tasks import library_enrichment_task

    settings = get_settings()

    def _reset_failed() -> int:
        with connect_sync(settings.database_url) as conn:
            repo = PgLibraryFileRepository(conn)
            count = repo.reset_failed_enrichments()
            conn.commit()
            return count

    # asyncio.to_thread avoids blocking the event loop during the sync DB call.
    # Note: this does not fix thread-pool exhaustion under load — async repos
    # remain the long-term fix (see async infrastructure plan).
    reset_count = await asyncio.to_thread(_reset_failed)
    library_enrichment_task()

    return RetryEnrichmentResult(
        reset=reset_count,
        message=(
            f"Reset {reset_count} failed file(s) to pending and queued enrichment."
        ),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```
uv run pytest tests/routers/test_tasks.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Run full typecheck**

```
uv run mypy backend --strict
```

Expected: No new errors.

- [ ] **Step 6: Commit**

```bash
git add backend/routers/tasks.py tests/routers/test_tasks.py
git commit -m "fix(routers): wrap retry_enrichment sync DB call in asyncio.to_thread"
```

---

## Task 2: Rename misnamed `mbid` parameters in ArtistRepository

**Context:** `get_by_id`, `mark_enhanced`, and `mark_enhancement_failed` all accept a parameter named `mbid` but query `artists.id` (the local UUID). The parameter name is semantically wrong. All callers use positional arguments so this is a non-breaking rename.

**Files:**
- Modify: `backend/repositories/artists.py`
- Modify: `backend/db/repositories/artists.py`
- Modify: `tests/fakes/artists.py`

- [ ] **Step 1: Update the ABC — rename mbid to artist_id in three methods**

In `backend/repositories/artists.py`, change lines 11, 22, 25:

```python
class ArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: Artist) -> Artist: ...

    @abstractmethod
    def get_by_id(self, artist_id: str) -> Artist | None: ...

    @abstractmethod
    def list_all(self) -> list[Artist]:
        """Return all artists for fuzzy-matching in artist_matching_service."""
        ...

    @abstractmethod
    def list_unenhanced(self) -> list[Artist]: ...

    @abstractmethod
    def mark_enhanced(self, artist_id: str) -> None: ...

    @abstractmethod
    def mark_enhancement_failed(self, artist_id: str, error: str) -> None: ...

    @abstractmethod
    def upsert_local_artist(self, name: str, normalized_name: str) -> str:
        """Create local artist or return existing by normalized_name.
        INSERT ON CONFLICT (normalized_name) DO NOTHING + retry-SELECT.
        Returns artist id.
        """
        ...

    @abstractmethod
    def upsert_musicbrainz_artist(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
    ) -> str:
        """Lookup by mbid or normalized_name, promote/create/reuse.

        The caller is responsible for computing ``normalized_name`` via
        ``backend.services.normalization.normalize_artist`` before calling
        this method; this keeps the repository layer free of service imports.

        Returns artist id (may be a promoted local UUID).
        """
        ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> Artist | None: ...
```

Note: `upsert_musicbrainz_artist` retains `mbid: str` because that parameter genuinely is a MusicBrainz ID.

- [ ] **Step 2: Update the Pg implementation**

In `backend/db/repositories/artists.py`, rename the same three parameters. Find the method signatures (they will have `mbid: str`) and rename to `artist_id: str` in `get_by_id`, `mark_enhanced`, `mark_enhancement_failed`. The SQL bodies query `WHERE id = ...` so also update the variable name used in the query parameter if needed.

Run first to find exact lines:

```
uv run grep -n "def get_by_id\|def mark_enhanced\|def mark_enhancement_failed" backend/db/repositories/artists.py
```

Then update each signature: `mbid: str` → `artist_id: str`. Update the variable reference in the query body from `mbid` to `artist_id`.

- [ ] **Step 3: Update the fake**

In `tests/fakes/artists.py`, rename the same three methods' parameters from `mbid` to `artist_id`:

```python
def get_by_id(self, artist_id: str) -> Artist | None:
    return self._data.get(artist_id)

def mark_enhanced(self, artist_id: str) -> None:
    if artist := self._data.get(artist_id):
        artist.needs_enhancement = False

def mark_enhancement_failed(self, artist_id: str, error: str) -> None:
    if artist := self._data.get(artist_id):
        artist.enhancement_error = error
```

- [ ] **Step 4: Run tests and typecheck**

```
uv run pytest tests/ -m "not integration and not slow" -v
uv run mypy backend --strict
```

Expected: All existing tests PASS. No new mypy errors.

- [ ] **Step 5: Commit**

```bash
git add backend/repositories/artists.py backend/db/repositories/artists.py tests/fakes/artists.py
git commit -m "refactor(repositories): rename misleading mbid params to artist_id in ArtistRepository"
```

---

## Task 3: Fix builtin `id` shadowing in LibraryFileRepository

**Context:** `get_by_id(self, id: UUID)` and `update_recording_link(self, id: UUID, ...)` shadow Python's builtin `id`. All callers use positional arguments, so this is a non-breaking parameter rename.

**Files:**
- Modify: `backend/repositories/library_files.py`
- Modify: `backend/db/repositories/library_files.py`
- Modify: `tests/fakes/library_files.py`

- [ ] **Step 1: Update the ABC**

In `backend/repositories/library_files.py`, change line 16 and line 35:

```python
@abstractmethod
def get_by_id(self, file_id: UUID) -> LibraryFile | None: ...
```

```python
@abstractmethod
def update_recording_link(
    self,
    file_id: UUID,
    recording_id: str | None,
    enrichment_status: EnrichmentStatus,
) -> None: ...
```

- [ ] **Step 2: Update the Pg implementation**

Find both method signatures in `backend/db/repositories/library_files.py`:

```
uv run grep -n "def get_by_id\|def update_recording_link" backend/db/repositories/library_files.py
```

For `get_by_id`: rename parameter `id` → `file_id`; update the query body if it uses `id` as the variable passed to the SQL query.

For `update_recording_link`: rename parameter `id` → `file_id`; update the query body similarly.

- [ ] **Step 3: Update the fake**

In `tests/fakes/library_files.py`, line 28:

```python
def get_by_id(self, file_id: UUID) -> LibraryFile | None:
    return self._data.get(file_id)
```

Search for any `update_recording_link` in the same file and rename its `id` parameter to `file_id`.

- [ ] **Step 4: Run tests and typecheck**

```
uv run pytest tests/ -m "not integration and not slow" -v
uv run mypy backend --strict
```

Expected: All existing tests PASS. No new mypy errors.

- [ ] **Step 5: Commit**

```bash
git add backend/repositories/library_files.py backend/db/repositories/library_files.py tests/fakes/library_files.py
git commit -m "refactor(repositories): rename id parameter to file_id to avoid builtin shadow"
```

---

## Task 4: Extract SyntheticWorkId into a domain module

**Context:** `_encode_synthetic_work_id` and `_decode_synthetic_work_id` in `backend/routers/library.py` are domain primitives (encoding a `(artist_id, track_title)` pair as a URL-safe string). They belong in the domain layer, not in an HTTP handler file.

**Files:**
- Create: `backend/domain/synthetic_work_id.py`
- Create: `tests/domain/test_synthetic_work_id.py`
- Modify: `backend/routers/library.py`

- [ ] **Step 1: Write failing tests for encode/decode**

Create `tests/domain/test_synthetic_work_id.py`:

```python
from backend.domain.synthetic_work_id import decode, encode


class TestEncode:
    def test_produces_syn_prefix(self) -> None:
        result = encode("artist-123", "My Song")
        assert result.startswith("syn_")

    def test_result_is_url_safe(self) -> None:
        result = encode("artist-123", "My Song (feat. X)")
        assert "+" not in result
        assert "/" not in result
        assert "=" not in result

    def test_different_inputs_produce_different_outputs(self) -> None:
        a = encode("artist-1", "Title A")
        b = encode("artist-1", "Title B")
        assert a != b


class TestDecode:
    def test_round_trip(self) -> None:
        artist_id = "550e8400-e29b-41d4-a716-446655440000"
        title = "Some Track Title"
        encoded = encode(artist_id, title)
        result = decode(encoded)
        assert result == (artist_id, title)

    def test_returns_none_for_non_synthetic_id(self) -> None:
        assert decode("mb_some-mbid-here") is None
        assert decode("plain-string") is None

    def test_handles_colon_in_title(self) -> None:
        artist_id = "artist-abc"
        title = "Part 1: The Beginning"
        encoded = encode(artist_id, title)
        result = decode(encoded)
        assert result == (artist_id, title)

    def test_returns_none_for_corrupted_input(self) -> None:
        assert decode("syn_notvalidbase64!!!") is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/domain/test_synthetic_work_id.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` — module does not exist yet.

- [ ] **Step 3: Create the domain module**

Create `backend/domain/synthetic_work_id.py`:

```python
from __future__ import annotations

import base64


def encode(artist_id: str, track_title: str) -> str:
    """Return a URL-safe synthetic work ID encoding artist_id and track_title."""
    raw = f"{artist_id}:{track_title}"
    return "syn_" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(work_id: str) -> tuple[str, str] | None:
    """Decode a synthetic work ID. Returns (artist_id, track_title) or None."""
    if not work_id.startswith("syn_"):
        return None
    encoded = work_id[4:]
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    try:
        raw = base64.urlsafe_b64decode(encoded).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    colon_idx = raw.find(":")
    if colon_idx == -1:
        return None
    return raw[:colon_idx], raw[colon_idx + 1:]
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/domain/test_synthetic_work_id.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 5: Update library.py to import from the domain module**

In `backend/routers/library.py`:

1. Remove the `import base64` line (line 3) — it is only used by the two helpers.
2. Remove lines 19–36 (both helper functions and their docstrings).
3. Add an import after the `from backend.domain.enums import CatalogSource` line:

```python
from backend.domain.synthetic_work_id import decode as decode_synthetic_work_id
from backend.domain.synthetic_work_id import encode as encode_synthetic_work_id
```

4. Update the two call sites (lines 416 and 470 in the original file — line numbers will shift after the removal):
   - `_encode_synthetic_work_id(...)` → `encode_synthetic_work_id(...)`
   - `_decode_synthetic_work_id(...)` → `decode_synthetic_work_id(...)`

- [ ] **Step 6: Run tests and typecheck**

```
uv run pytest tests/ -m "not integration and not slow" -v
uv run mypy backend --strict
```

Expected: All tests PASS. No new mypy errors.

- [ ] **Step 7: Commit**

```bash
git add backend/domain/synthetic_work_id.py tests/domain/test_synthetic_work_id.py backend/routers/library.py
git commit -m "refactor(domain): extract SyntheticWorkId encode/decode from router into domain module"
```

---

## Task 5: Rename inconsistently named task modules

**Context:** All task modules use plural `_tasks.py` suffix except `normalize_backfill_task.py` (singular) and `library_tasks.py` (name doesn't signal "scan"). Rename both for consistency.

**Files:**
- Rename: `backend/tasks/normalize_backfill_task.py` → `backend/tasks/normalize_backfill_tasks.py`
- Rename: `backend/tasks/library_tasks.py` → `backend/tasks/library_scan_tasks.py`
- Modify: `backend/tasks/huey_app.py`
- Modify: `backend/routers/library.py`
- Modify: `tests/services/test_scan_progress.py`
- Modify: `tests/tasks/test_scan_enrichment_chain.py`
- Modify: `tests/tasks/test_library_tasks.py`

- [ ] **Step 1: Rename normalize_backfill_task.py**

```bash
git mv backend/tasks/normalize_backfill_task.py backend/tasks/normalize_backfill_tasks.py
```

- [ ] **Step 2: Update the huey_app.py import for normalize_backfill_tasks**

In `backend/tasks/huey_app.py`, change line 27:

```python
import backend.tasks.normalize_backfill_tasks  # noqa: F401, E402
```

- [ ] **Step 3: Rename library_tasks.py**

```bash
git mv backend/tasks/library_tasks.py backend/tasks/library_scan_tasks.py
```

- [ ] **Step 4: Update all imports of library_tasks → library_scan_tasks**

Four files reference `backend.tasks.library_tasks`:

**`backend/tasks/huey_app.py`** line 24:
```python
import backend.tasks.library_scan_tasks  # noqa: F401, E402
```

**`backend/routers/library.py`** line 16:
```python
from backend.tasks.library_scan_tasks import library_scan_task
```

**`tests/services/test_scan_progress.py`** — three occurrences of lazy import inside test bodies:
```python
from backend.tasks.library_scan_tasks import library_scan_task
```
(Replace all three occurrences.)

**`tests/tasks/test_scan_enrichment_chain.py`** — two occurrences:
```python
from backend.tasks.library_scan_tasks import library_scan_task
```

**`tests/tasks/test_library_tasks.py`** — seven occurrences:
```python
from backend.tasks.library_scan_tasks import _run_scan
```

- [ ] **Step 5: Rename the test file to match**

```bash
git mv tests/tasks/test_library_tasks.py tests/tasks/test_library_scan_tasks.py
```

- [ ] **Step 6: Run the full fast test suite**

```
uv run pytest tests/ -m "not integration and not slow" -v
```

Expected: All tests PASS. The renamed file `tests/tasks/test_library_scan_tasks.py` should be discovered and run.

- [ ] **Step 7: Run typecheck**

```
uv run mypy backend --strict
```

Expected: No errors.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(tasks): rename library_tasks→library_scan_tasks and normalize_backfill_task→normalize_backfill_tasks"
```

---

## Verification

After all five tasks are committed, run the full fast suite one final time:

```
uv run pytest tests/ -m "not integration and not slow" -v
uv run mypy backend --strict
uv run ruff check .
```

All three must be clean before opening a PR.

---

## Self-Review Checklist

- [x] **Spec coverage:** All 5 items from Plan A are covered (sync escape hatch, ArtistRepository param renames, LibraryFile builtin shadow, synthetic_work_id extraction, task module renames).
- [x] **Placeholder scan:** No TBD/TODO/placeholder steps. Every code block shows the complete change.
- [x] **Type consistency:** `encode`/`decode` names in Task 4 match their usage in library.py. `artist_id` used consistently across ABC, Pg impl, and fake. `file_id` used consistently across ABC, Pg impl, and fake.
- [x] **Callers checked:** All callers of renamed parameters use positional args — no keyword call sites to update.
- [x] **Huey registration:** Both task file renames update `huey_app.py` — tasks will remain registered with the worker.
