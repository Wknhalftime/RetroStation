# Work Deduplication & Version Matching

> **Scope:** Canonical musical works — strip version tags so one Work row represents
> one composition. Not related to scan/Huey task deduplication.

## Context

Multiple versions of the same musical work (e.g., "You Oughta Know", "You Oughta Know (Live/Unplugged)", "You Oughta Know (Jimmy the Saint Blend)") are stored as separate, independent work records. This makes deduplication difficult and inflates the works catalog with redundant entries.

The goal is a **one work per composition** model where version information lives on Recordings, not Works. The grouping service strips version tags before matching, so all versions of the same composition converge on a single canonical work.

## Approach: Strip-Before-Match

Modify `grouping_service.assign_work()` to extract and strip version tags from titles before fuzzy matching. Works store the canonical/base title; Recordings carry the version type.

Existing normalization infrastructure (`extract_version_tags`, `extract_dash_version`, `detect_embedded_remix`, `classify_version_descriptor`) is reused — no new parsing logic needed.

## Design

### 1. Grouping Service Changes

**File:** `backend/services/grouping_service.py`

#### 1a. Version extraction (before Step 3)

Extract version info from the raw title:

1. `extract_version_tags(raw_title)` -> `(base_title, tags)`
2. If no tags: `extract_dash_version(raw_title)` -> `(base_title, dash_tag)`
3. If no dash tag: `detect_embedded_remix(raw_title)` -> `(base_title, embed_tag)`
4. Classify extracted tags -> `VersionType` (LIVE, REMIX, ACOUSTIC, etc.)
5. If no tags extracted or all classify as UNKNOWN -> `version_type = ORIGINAL`, `base_title = raw_title`

**Safeguard:** Only tags classified as non-UNKNOWN by `classify_version_descriptor` are stripped. Parentheticals that are part of the actual title (e.g., "(You Drive Me) Crazy") are preserved because they don't match any known version type.

**Multiple tags:** When multiple version tags are extracted (e.g., "Live / Unplugged"), classify the combined descriptor string. `classify_version_descriptor` checks `\blive\b` before `unplugged`, so "Live / Unplugged" classifies as **LIVE**. The first matching `VersionType` in `classify_version_descriptor`'s priority order wins. If multiple parenthetical groups yield different types, use the first non-UNKNOWN classification.

**Work title storage:** Works always store the base/canonical title (e.g., "You Oughta Know"). The full versioned title is preserved on the Recording.

#### 1b. Fuzzy match pipeline order

The pipeline for grouping is:

1. Extract version tags from `raw_title` -> `base_title` + `version_type`
2. `normalize_title(base_title)` -> `norm_base` (for fuzzy matching)
3. Fuzzy-match `norm_base` against **`works.title`** (not `library_files.normalized_title`)

**Candidate title source change:** Currently `get_candidates_by_artist` returns `MIN(normalized_title)` from `library_files` — a weak proxy for the canonical title. Replace this with a new method on `WorkRepository` that queries `works` directly.

New method on `WorkRepository`:

```python
def get_candidates_by_normalized_artist(
    self, normalized_artist_name: str, limit: int = 100,
) -> list[tuple[str, str]]:
    """Return (work_id, work_title) pairs for fuzzy matching.

    Deduplicates by work_id (DISTINCT ON / GROUP BY) so each work
    appears at most once regardless of how many files reference it.
    Ordered by work title for stable results.
    """
```

Query:

```sql
SELECT DISTINCT w.id, w.title
FROM works w
JOIN library_files lf ON lf.work_id = w.id
WHERE lf.normalized_artist_name = %s
ORDER BY w.title
LIMIT %s
```

The `DISTINCT` ensures each work appears once even if many files reference it. The caller normalizes each `w.title` via `normalize_title()` in Python for fuzzy comparison.

**Performance note:** This normalizes up to `limit` titles in Python per call. `normalize_title()` runs several regexes but is pure and fast. At 100 candidates per file, this is acceptable for v1. If profiling shows it's a bottleneck, add a `normalized_title` column to `works` pre-computed at creation time. Do not add this column speculatively.

**Retirement of `get_candidates_by_artist`:** The existing `LibraryFileRepository.get_candidates_by_artist` method becomes dead code after this change. Remove it from the ABC, `PgLibraryFileRepository`, and `FakeLibraryFileRepository` as part of this spec. No other consumer calls it.

#### 1c. `library_files.normalized_title` remains unchanged

`library_files.normalized_title` continues to store the full normalized title (with version tags). It is used by other consumers (identity matching tier-2, ingestion signatures). Only the grouping fuzzy match uses the stripped base title, computed at match time.

#### 1d. Return type change

`assign_work` returns a `GroupingResult` dataclass instead of `str | None`:

```python
@dataclass
class GroupingResult:
    work_id: str
    recording_id: str | None = None
```

**Location:** Define `GroupingResult` in `backend/services/grouping_service.py` (service-layer DTO, not a domain entity — does not belong in `domain/models.py`).

**Return semantics:**
- Returns `None` when the file has no artist/title (unchanged behavior)
- Returns `GroupingResult(work_id=..., recording_id=...)` on success
- `work_id` is always non-empty on the success path (a `GroupingResult` with empty `work_id` is a bug)
- A `GroupingResult` instance is always truthy; `None` is falsy. Callers continue to use `if result:` to detect success, but must access `result.work_id` / `result.recording_id` for values

**Caller migration (critical):** Every call site and test must be updated in a single pass:

| Location | Current pattern | New pattern |
|----------|----------------|-------------|
| `library_tasks.py` ~line 131 | `work_id = assign_work(...)` then `if work_id:` | `result = assign_work(...)` then `if result:` use `result.work_id` |
| `library_watcher_tasks.py` ~line 152 | Same | Same |
| `test_grouping_service.py` (10+ tests) | `assert result == "some-id"` | `assert result.work_id == "some-id"` |
| `test_grouping_e2e.py` (10+ calls) | `w1 = assign_work(...); file_repo.update_work_id(f.id, w1)` | `r1 = assign_work(...); file_repo.update_work_id(f.id, r1.work_id)` |

#### 1e. Hash and MBID shortcut paths

Both shortcut paths (Steps 1 and 2) currently return `work_id` without setting `recording_id`. Updated behavior:

**Hash shortcut (Step 1):** Copy both `work_id` AND `recording_id` from the existing file: `GroupingResult(work_id=existing.work_id, recording_id=existing.recording_id)`. `recording_id` may be `None` on files grouped before the data reset — safe because `None` is handled by callers and the data reset precedes production use.

**MBID shortcut (Step 2):** The recording already exists in the `recordings` table (from MB enrichment). Return `GroupingResult(work_id=recording.work_id, recording_id=recording.id)`.

**Validation:** Hash shortcut copies `recording_id` from a sibling file. That recording's `work_id` should equal the copied `work_id`. In steady state (post-reset), this is guaranteed because both were set by the same grouping pass. No explicit cross-check needed for v1, but log a warning if they diverge.

#### 1f. Dual writers — library_tasks and library_watcher_tasks

Both `library_tasks.py` and `library_watcher_tasks.py` call `assign_work` and must be updated to persist `recording_id` alongside `work_id`:

```python
result = assign_work(lf, ...)
if result:
    library_file_repo.update_work_id(lf.id, result.work_id)
    if result.recording_id:
        library_file_repo.update_recording_link(
            lf.id, result.recording_id, EnrichmentStatus.PENDING,
        )
```

**No new `update_recording_id` method.** Reuse the existing `update_recording_link(id, recording_id, enrichment_status)` from `LibraryFileRepository`. Grouping sets `enrichment_status=PENDING` (the file is grouped but not yet enriched). Enrichment later overwrites with `ENRICHED` and a MB recording ID. This is correct because `update_recording_link` is a targeted UPDATE that only touches `recording_id` and `enrichment_status` — it does not interact with the `upsert_write_only` CASE logic.

**Enrichment gating check:** `recordings.list_needing_enhancement` queries `WHERE needs_enhancement = TRUE`. Local recordings created by `get_or_create_local` set `needs_enhancement = FALSE`, so they are not picked up for MB enrichment. No gating issue.

#### 1g. Step 4: Create work with base_title

When no match is found (Step 4), the work must be created with the **stripped base title**, not the raw title:

```python
# Step 4: Create local work
artist_id = artist_repo.upsert_local(raw_artist, norm_artist)
work_id = work_repo.create_local(base_title, artist_id)  # NOT raw_title
```

This ensures works always store the canonical composition title. The full versioned title is preserved on the Recording created in Section 2.

### 2. Recording Creation During Grouping

**Files:** `backend/services/grouping_service.py`, `backend/repositories/recordings.py`, `backend/db/repositories/recordings.py`

Currently `assign_work` doesn't create Recordings. This changes:

1. After matching/creating a work:
   - If version tags were extracted -> classify to `VersionType`, create/reuse a Recording with that `version_type` and the full original title
   - If no version tags -> create/reuse a Recording with `version_type=ORIGINAL`
2. Return `GroupingResult(work_id, recording_id)` so callers can persist both links

**New repository method:** `RecordingRepository.get_or_create_local(work_id, version_type, title) -> str`

Single method combining lookup + insert:

```sql
INSERT INTO recordings (id, title, work_id, version_type, needs_enhancement)
VALUES (%s, %s, %s, %s, FALSE)
ON CONFLICT (work_id, version_type) DO NOTHING
RETURNING id
```

If `RETURNING` is empty (row already existed), follow with:

```sql
SELECT id FROM recordings WHERE work_id = %s AND version_type = %s
```

This is race-safe: concurrent inserts resolve via `ON CONFLICT`, and the fallback SELECT always finds the winner. `needs_enhancement = FALSE` prevents MB enrichment tasks from picking up local recordings.

**Reuse semantics (v1 decision):** `(work_id, version_type)` is the reuse key. Multiple distinct performances of the same version type (e.g., two different live recordings) collapse into one Recording row. This is acceptable for v1 — the Recording represents a version *kind*, not a specific performance. Product sign-off: if a playlist requests "the live version," we return the SongMaster/FormatOverride preferred file for that work, not a specific live performance. If finer granularity is needed later, a secondary key (e.g., title suffix hash) can be added.

**Local recording IDs:** UUID, same pattern as local works.

**Migration (0012):** Add a `UNIQUE` constraint on `recordings(work_id, version_type)` — not just an index. This enforces the v1 reuse contract at the database level and enables the `ON CONFLICT` upsert pattern:

```sql
-- 0012_version_grouping.sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_recordings_work_version
    ON recordings (work_id, version_type);
```

### 3. SongMaster Interaction

When `assign_work` matches an **existing** work (Steps 1-3), it does NOT update the SongMaster. The existing preferred file remains. SongMaster is only created when a **new** work is created (Step 4), using the first file as the preferred file.

Rationale: adding a new version (e.g., a live recording) to an existing work should not change which file is the default playback choice. SongMaster selection can be changed manually or by a future auto-selection algorithm that considers version preferences.

### 4. Enrichment Interaction

`library_enrichment_service` may later overwrite `recording_id` and `work_id` with MusicBrainz data. When this happens:

- Local Recording rows created during grouping may become orphaned (no files reference them)
- The work may switch from local origin to MB origin
- This is acceptable — `delete_if_empty` already handles cleaning up unreferenced works

**Orphaned recording cleanup (committed deliverable):** Add a cleanup step to the enrichment task's post-pass in `mb_enrichment_tasks.py`, after the recording enrichment loop:

```sql
DELETE FROM recordings r
WHERE NOT EXISTS (
    SELECT 1 FROM library_files lf WHERE lf.recording_id = r.id
)
AND r.needs_enhancement = FALSE
AND r.enhanced_at IS NULL
```

The `NOT EXISTS` form is preferred over `NOT IN` for NULL safety and performance (avoids subquery materialization). The `needs_enhancement = FALSE AND enhanced_at IS NULL` predicate targets only locally-created recordings (never enhanced by MB) — it avoids deleting MB recordings that temporarily have no files during concurrent enrichment.

**Transaction boundaries:** The cleanup runs after the enrichment commit, in its own transaction. A recording is only deleted if no file references it after enrichment has finished writing. This prevents racing with concurrent grouping tasks that may be about to reference a recording.

Local grouping UUIDs are **not permanent** — they are placeholders until MB enrichment provides canonical IDs.

### 5. Split/Merge/Reassign Audit

The `merge_works`, `split_work`, and `reassign_file_work` routes in `library.py` move `recordings.work_id`. Under the new shared-Recording model (one Recording per version kind, multiple files per Recording), these operations need adjustment.

**`merge_works` (line 1031):**

Current behavior moves all recordings from source works to target. Under shared-Recording semantics, merging may create duplicate `(work_id, version_type)` pairs if both source and target have a recording of the same version type.

Fix — transactional recipe:
1. For each source recording, check if target already has a recording with the same `version_type`
2. If yes (conflict): reassign all `library_files.recording_id` from source recording to target recording, then DELETE the source recording
3. If no conflict: UPDATE `recordings.work_id` to target (existing behavior)
4. After all recordings processed: delete source song masters, delete source works, recalculate target song master

Extract a helper `_consolidate_recordings(conn, source_work_ids, target_work_id)` to keep `merge_works` readable.

**`split_work` (line 1155):**

Current behavior moves a Recording to a new work. Under shared-Recording semantics, moving the Recording moves **all** files sharing it, not just the one file being split.

Fix: create a **new** Recording for the split file rather than moving the existing one.
1. Read the existing Recording's columns: `title`, `version_type`, `duration_ms`
2. Create a new Recording with the same values (new UUID, `needs_enhancement=FALSE`)
3. Update only the split file's `recording_id` to the new Recording
4. Update the split file's `work_id` to the new work
5. Leave the original Recording on the old work with its remaining files

Columns to clone: `title`, `version_type`, `duration_ms`. Columns to reset: `id` (new UUID), `needs_enhancement` (FALSE), `enhanced_at` (NULL), `enhancement_error` (NULL), `embedding` (NULL).

**`reassign_file_work` (line 1261):**

Same issue as `split_work` — moving the Recording moves all files sharing it.

Fix: check if the Recording is shared (other files reference it):
1. `SELECT COUNT(*) FROM library_files WHERE recording_id = %s AND id != %s`
2. If shared: create a new Recording on the target work (clone fields as above), update only this file's `recording_id`
3. If not shared: UPDATE `recordings.work_id` to the target work (existing behavior)
4. In both cases: check for `(work_id, version_type)` conflict on the target work — if the target already has a recording of the same version type, merge files into the existing recording instead of creating/moving

### 6. Identity Matching Fix (In-Scope)

**File:** `backend/services/identity_matching_service.py`

Two fixes:

**Tier 2 (line 142-143):** Currently appends `best_file.recording_id` to a list documented as `work_ids`. With `recording_id` now set on all grouped files, this bug triggers at much higher frequency. Fix: use `library_files.work_id` directly:

```python
if best_file.work_id:
    work_ids.append(best_file.work_id)
```

**Tier 0 (line 78-80):** Currently a no-op (`pass`). Tier 0 matches never contribute `work_ids` for downstream master selection. Fix: append `lib_file.work_id` when present:

```python
if lib_file.work_id:
    work_ids.append(lib_file.work_id)
```

This ensures downstream `recalculate_masters_for_works` receives correct work IDs from both matching tiers.

### 7. API & Frontend Changes

**Backend — WorkSummary schema** (`backend/routers/library.py`):

Add `version_types` with a default empty list for backwards compatibility:

```python
class WorkSummary(BaseModel):
    id: str
    title: str
    recording_count: int
    has_master: bool
    mbid: str | None = None
    origin: str = "local"
    version_types: list[str] = []
```

**Artist detail query:** Use `array_agg(DISTINCT r.version_type)` via a LEFT JOIN on `recordings`:

```sql
SELECT w.id, w.title, w.mbid, w.origin,
       COUNT(DISTINCT lf.id) AS recording_count,
       COUNT(DISTINCT sm.id) AS master_count,
       array_agg(DISTINCT r.version_type)
           FILTER (WHERE r.version_type IS NOT NULL) AS version_types
FROM works w
LEFT JOIN library_files lf ON lf.work_id = w.id
LEFT JOIN song_masters sm ON sm.work_id = w.id
LEFT JOIN recordings r ON r.work_id = w.id
WHERE w.artist_id = %s
GROUP BY w.id, w.title, w.mbid, w.origin
ORDER BY w.title
```

**Index validation:** The query benefits from: `idx_works_artist` (existing), `idx_library_files_work_id` (existing), and the new `uq_recordings_work_version` index (covers `r.work_id` join). Run `EXPLAIN ANALYZE` on realistic data to confirm no seq scans.

**`array_agg` NULL handling:** PostgreSQL's `array_agg ... FILTER (WHERE ...)` returns `NULL` (not `[]`) when no qualifying rows exist. The row-mapping code must handle this:

```python
version_types=row.get("version_types") or []
```

**Fallback path (synthetic works):** When no `works` rows exist and the API falls back to grouping by `library_files.track_title`, `version_types` defaults to `[]`. No badges rendered — version info is unavailable without recordings.

**Badge ordering:** Sort `version_types` in a fixed display order: ORIGINAL first, then alphabetically. Defined as a constant in the frontend. Python sort on the `array_agg` output is alphabetical by string value; frontend re-sorts using the display-order constant.

**Frontend — Schema** (`frontend/src/lib/schemas/artists.ts`):

```typescript
export const WorkSummarySchema = z.object({
  id: z.string(),
  title: z.string(),
  recording_count: z.number(),
  has_master: z.boolean(),
  version_types: z.array(z.string()).default([]),
})
```

**Frontend — Version badges:**

Artist detail page renders each `version_type` as an inline badge next to the work title:

> You Oughta Know `[Studio]` `[Live]` `[Remix]`

**Complete display label mapping for all VersionType values:**

| VersionType | Badge Label | Notes |
|-------------|-------------|-------|
| ORIGINAL | Studio | UX simplification; some tracks may be "album version" |
| LIVE | Live | |
| REMASTER | Remaster | |
| REMIX | Remix | |
| RADIO_EDIT | Radio Edit | |
| DEMO | Demo | |
| ACOUSTIC | Acoustic | |
| EXTENDED | Extended | |
| INSTRUMENTAL | Instrumental | |
| EXPLICIT | Explicit | |
| COVER | Cover | |
| EDITION | Edition | |
| ALTERNATE | Alt | |
| FORMAT | Format | |
| UNKNOWN | — | Do not render; indicates bad data |
| OTHER | — | Do not render; indicates bad data |

- Muted/secondary pill styling
- Informational only, no click behavior in this iteration
- Cap at 5 visible badges; if more, show "+N more" overflow

**Work detail page:** Already shows recordings with `version_type` — no changes needed.

**Structured logging:** Add `logger.info("recording_created", ...)` and `logger.info("recording_reused", ...)` inside `assign_work` when the Recording logic runs, following the existing `structlog` convention. Use `info` level (not `debug`) for operational visibility in production — recording creation/reuse is a key deduplication metric.

### 8. Data Reset

Clean wipe and re-import instead of a migration script:

1. Drop and recreate schema
2. Re-run migrations (including new UNIQUE constraint on `recordings(work_id, version_type)`)
3. Re-scan library (triggers updated `assign_work` with version-aware logic)

**Pre-condition check:** This reset strategy is only safe when:
- `SELECT count(*) FROM song_masters WHERE selection_method = 'manual'` returns 0
- `SELECT count(*) FROM format_overrides` returns 0

If either is non-zero, manual overrides exist and a migration strategy must be designed first. Document this check in operator runbook / startup script.

All canonical data is derived from audio file metadata. No manual overrides to preserve in the current development database.

### 9. Rescan / Hash-Change Behavior

When a file is rescanned, `upsert_write_only` handles `recording_id` and `work_id` differently:

- **`recording_id`** (line 190): Always set to `EXCLUDED.recording_id` — **no** hash-change CASE. On a rescan where the file object has `recording_id=NULL` (freshly scanned), this resets `recording_id` to NULL regardless of hash change.
- **`work_id`** (lines 205-209): Uses a `CASE` — preserved when hash is unchanged, reset to NULL when hash changes.

**Implications:** After a rescan of an unchanged file, `recording_id` is reset to NULL but `work_id` is preserved. The subsequent grouping pass re-runs `assign_work` only on files where `work_id IS NULL` (per `library_tasks.py` line ~121 `if lf.work_id is not None: continue`). So for unchanged files, grouping is skipped and `recording_id` stays NULL until a full re-group.

**For the data reset scenario:** This is not an issue because the reset wipes everything and re-scans from scratch. For incremental rescans (watcher), the watcher calls `assign_work` unconditionally and then sets both `work_id` and `recording_id` via the caller pattern in Section 1f. The `upsert_write_only` NULL is overwritten by the subsequent `update_work_id` + `update_recording_link` calls.

**Action for v1:** The current behavior is correct for both paths (full reset and watcher). If future work needs `recording_id` preserved across no-change rescans, add a `CASE` on hash change (like `work_id` has). Not needed now.

## Key Files

| File | Change |
|------|--------|
| `backend/services/grouping_service.py` | Strip version tags, create Recordings, return `GroupingResult`, define `GroupingResult` dataclass |
| `backend/services/normalization.py` | No changes (existing extraction functions reused) |
| `backend/services/identity_matching_service.py` | Fix `work_ids` list: use `lib_file.work_id` for both Tier 0 and Tier 2 |
| `backend/repositories/works.py` | Add `get_candidates_by_normalized_artist` |
| `backend/db/repositories/works.py` | Implement `get_candidates_by_normalized_artist` (DISTINCT query) |
| `backend/repositories/recordings.py` | Add `get_or_create_local` abstract method |
| `backend/db/repositories/recordings.py` | Implement `get_or_create_local` (INSERT ON CONFLICT + fallback SELECT, `needs_enhancement=FALSE`) |
| `backend/repositories/library_files.py` | Remove `get_candidates_by_artist` abstract method |
| `backend/db/repositories/library_files.py` | Remove `get_candidates_by_artist` implementation |
| `backend/tasks/library_tasks.py` | Persist `recording_id` via `update_recording_link` from `GroupingResult` |
| `backend/tasks/library_watcher_tasks.py` | Persist `recording_id` via `update_recording_link` from `GroupingResult` |
| `backend/tasks/mb_enrichment_tasks.py` | Add orphaned recording cleanup post-pass |
| `backend/routers/library.py` | Add `version_types` to `WorkSummary`, update query, fix `split_work`/`merge_works`/`reassign_file_work` for shared-Recording semantics, extract `_consolidate_recordings` helper |
| `backend/db/migrations/0012_version_grouping.sql` | UNIQUE constraint on `recordings(work_id, version_type)` |
| `frontend/src/lib/schemas/artists.ts` | Add `version_types` to `WorkSummarySchema` |
| Artist detail component (frontend) | Render version badges with complete label mapping |
| `tests/services/test_grouping_service.py` | Update all assertions to `result.work_id`; add version-aware tests |
| `tests/services/test_grouping_e2e.py` | Update all `assign_work` return value usage; add version-aware tests |
| `tests/fakes/recordings.py` | Add `get_or_create_local` to fake (scan `_data` for match, insert UUID if not found) |
| `tests/fakes/works.py` | Add `get_candidates_by_normalized_artist` to fake |
| `tests/fakes/library_files.py` | Remove `get_candidates_by_artist` from fake |

## Testing

**Unit tests (grouping_service):**
- "You Oughta Know (Live/Unplugged)" matches existing work "You Oughta Know", returns same `work_id`
- Recording with `version_type=LIVE` created for versioned file (per `classify_version_descriptor` priority: `\blive\b` matches before `unplugged`)
- File with no version tags creates Recording with `version_type=ORIGINAL`
- "(You Drive Me) Crazy" is NOT stripped (not a known version type)
- Two files with different version types share one work, get separate recordings
- Hash shortcut returns both `work_id` and `recording_id`
- MBID shortcut returns both `work_id` and `recording_id`
- Concurrent: two files for same work, different versions, processed sequentially -> one work, two recordings
- Step 4 creates work with `base_title`, not `raw_title` — assert `work.title == "Brand New Song"` when input is `"Brand New Song (Live)"`
- All existing tests updated to compare `result.work_id` instead of `result`

**Watcher parity tests:**
- `library_watcher_tasks` persists both `work_id` and `recording_id` from `GroupingResult`
- Same behavior as `library_tasks` grouping pass

**Enrichment interaction tests:**
- File with local grouping then successful MB enrichment -> `recording_id` and `work_id` updated to MB IDs
- Orphaned local recording cleaned up by post-enrichment cleanup (NOT EXISTS query)
- `list_needing_enhancement` does NOT return local recordings (`needs_enhancement=FALSE`)

**Split/merge/reassign tests:**
- `merge_works` with duplicate `(work_id, version_type)`: files reassigned to target recording, source recording deleted, SongMaster recalculated
- `split_work` on a shared Recording: new Recording created for split file (clones title, version_type, duration_ms; resets id, needs_enhancement, enhanced_at, embedding), original Recording stays with old work
- `reassign_file_work` on a shared Recording: new Recording created on target work, only reassigned file's `recording_id` updated
- `reassign_file_work` on an unshared Recording: existing Recording moved to target work (existing behavior)
- All three operations handle `(work_id, version_type)` conflicts on target

**Identity matching tests:**
- Tier 2: `work_ids` list contains `best_file.work_id` (not `recording_id`)
- Tier 0: `work_ids` list contains `lib_file.work_id` when present
- Integration: `recalculate_masters_for_works` receives valid work IDs from both tiers

**API tests:**
- `WorkSummary.version_types` returns correct sorted list for multi-recording works
- `version_types` defaults to `[]` when no recordings exist (fallback/synthetic path)
- `array_agg` NULL -> `[]` conversion works correctly
- Work detail shows recordings grouped under one work

**Fake implementation tests:**
- `test_fakes_implement_abcs.py` passes with new abstract methods and removed `get_candidates_by_artist`
- `FakeRecordingRepository.get_or_create_local(work_id, version_type, title)`: scans `self._data.values()` for matching `(work_id, version_type)`, returns existing ID if found, otherwise generates UUID and inserts
- `FakeWorkRepository.get_candidates_by_normalized_artist`: filters works by artist match against stored files, returns `(work_id, title)` pairs

**Manual verification:**
After clean wipe + re-import, check Alanis Morissette:
- "That I Would Be Good" appears once with version badges
- "You Oughta Know" appears once with version badges

## Resolution Behavior

When a playlist entry requests a work:
- **With version qualifier** (e.g., "live") -> match to the Recording with that `version_type`
- **Without qualifier** -> resolve via existing chain: FormatOverride (if station has one) > SongMaster preferred file > fallback
- Identity tier-2 matching still uses full `normalize_title` against `library_files` — if a broadcast log says "You Oughta Know" (no qualifier) and the library only has a live version, existing fuzzy matching still works because the base titles match. The version resolution is downstream in the SongMaster/FormatOverride chain.
