# Local-First Song Grouping — Design Spec

## Context

RetroStation groups music entities into "works" (compositions) so that all versions of a song
(radio edit, live, remaster, remix) live under one umbrella. The current system is
MusicBrainz-first: `works.id` is a TEXT MBID, so no group can exist without MB data. Files
without MBID tags (the majority of real-world radio libraries) cannot be grouped at all.

This design flips the architecture to **local-first**: grouping happens immediately from file
metadata, and MusicBrainz enrichment arrives later as a non-blocking background enhancement.

**Primary goal:** Version linking — broadly collect all versions of a composition under one work.
False negatives (separate works that should merge) are tolerable (user merges manually); false
positives (two different songs merged) are harder to undo and should be minimized.

**Scope (v1):** Library files only. Playlist log matching against local works is deferred to v2.
The existing artist_matching -> identity_matching pipeline continues to operate for MB-matched
entities. For local-only works, log entries remain unmatched until either (a) MB enrichment
promotes the work and the existing pipeline picks it up, or (b) v2 adds direct log -> work
matching.

---

## 1. API Contract — Identifier Semantics

### 1.1 Breaking change

`works.id` and `artists.id` are no longer guaranteed to be MusicBrainz UUIDs. They are now
**opaque string primary keys**. Clients must not parse, validate, or assume the format of these
IDs.

**New invariant:** Use `mbid` (nullable) when you need a MusicBrainz-specific identifier. Use
`id` for all internal references, FKs, and API resource paths. UUID-shaped strings are **not
guaranteed** for `id` — clients with regex validation on IDs must be updated.

**UUID generation:** All local IDs are generated in Python via `str(uuid.uuid4())` and stored
as TEXT. SQL pseudo-code in this spec uses `gen_uuid()` as shorthand for this pattern.

### 1.2 API response shape

All work/artist API responses include:
```json
{
  "id": "opaque-string-pk",
  "mbid": "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d" | null,
  "origin": "local" | "musicbrainz",
  ...
}
```

### 1.3 Internal audit

Code that assumes `works.id` is an MBID:
- `mb_client.py`: Uses `mbid` parameter, not `works.id`. **No change needed.**
- `library_enrichment_service.py`: **NEEDS SURGERY** — See Section 5.5.
- `identity_matching_service.py`: Uses `artist_mbid` for candidate lookup. **No change needed.**
- `song_masters.work_id`, `format_overrides.work_id`: FK only, never parsed. **No change.**
- `matches.target_id` with `target_type=WORK`: Stores work ID as opaque ref. **No change.**
- `backend/routers/library.py` Pydantic response models: **Must add `mbid`, `origin` fields.**
- Frontend Zod schemas: **Must add `mbid | null` and `origin` fields.**

---

## 2. Schema Changes (Migration 0011)

### 2.1 `artists` table

```sql
ALTER TABLE artists ADD COLUMN mbid TEXT UNIQUE;
ALTER TABLE artists ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';
ALTER TABLE artists ADD COLUMN normalized_name TEXT;

-- Backfill existing MB-sourced rows:
UPDATE artists SET mbid = id, origin = 'musicbrainz';

-- Unique constraint for concurrent-safe artist creation:
CREATE UNIQUE INDEX idx_artists_norm_name ON artists(normalized_name);
```

- `mbid`: nullable. Populated when MB enrichment discovers the real MBID.
- `origin`: `'local'` or `'musicbrainz'`.
- `normalized_name`: output of `normalize_artist()` on `name`. Used for consistent artist
  resolution (fixes the `lower(name)` vs `normalize_artist()` inconsistency flagged in review).
- `id` remains TEXT PK. Local artists use generated UUID strings.
- `sort_name` for local artists is set to `name` (same value) until enrichment updates it.
- **`needs_enhancement`:** Local-origin inserts explicitly set `needs_enhancement = FALSE`.
  The enhancement queue (`list_needing_enhancement()`) only processes MB-origin entities.
  This prevents futile MB lookups for artists that have no MBID.

### 2.2 `works` table

```sql
ALTER TABLE works ADD COLUMN mbid TEXT UNIQUE;
ALTER TABLE works ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';

-- Backfill:
UPDATE works SET mbid = id, origin = 'musicbrainz';
```

Same pattern as artists. `needs_enhancement = FALSE` for local-origin works.
No `canonical_file_id` — use existing `song_masters` table.

### 2.3 `library_files` table

```sql
ALTER TABLE library_files ADD COLUMN artist_name TEXT;
ALTER TABLE library_files ADD COLUMN normalized_artist_name TEXT;
ALTER TABLE library_files ADD COLUMN normalized_title TEXT;
ALTER TABLE library_files ADD COLUMN work_id TEXT REFERENCES works(id);

-- Indexes:
CREATE INDEX idx_library_files_file_hash ON library_files(file_hash);
CREATE INDEX idx_library_files_norm_artist ON library_files(normalized_artist_name);
CREATE INDEX idx_library_files_work_id ON library_files(work_id);
```

- `artist_name`: raw artist name from audio tags (populated during scan via mutagen).
- `normalized_artist_name`: output of `normalize_artist()`, indexed for O(1) lookup.
- `normalized_title`: output of `normalize_title()` on `track_title`.
- `work_id`: direct FK to works. Enables local-first grouping without the
  `library_files -> recordings -> works` chain that requires MBIDs.
- **`idx_library_files_file_hash`:** New index on `file_hash` — Step 1 (hash shortcut) requires
  this for O(1) lookup. Without it, the query is a full table scan. (No existing migration
  creates this index.)

### 2.4 `upsert` preservation of `work_id` during re-scan

The existing `PgLibraryFileRepository.upsert()` uses `ON CONFLICT (file_path) DO UPDATE SET`
which overwrites every column with `EXCLUDED.*`. Without protection, a re-scan would NULL out
`work_id` (since `extract_tags()` produces a `LibraryFile` without a `work_id`).

**Fix:** Add conditional preservation in the ON CONFLICT clause, same pattern as
`enrichment_status`:

```sql
work_id = CASE
    WHEN library_files.file_hash = EXCLUDED.file_hash
    THEN library_files.work_id           -- unchanged file: preserve grouping
    ELSE NULL                            -- hash changed: re-group on next pass
END,
-- Use NULLIF to prevent empty strings from blocking Step 3 matching:
normalized_artist_name = COALESCE(
    NULLIF(TRIM(EXCLUDED.normalized_artist_name), ''),
    library_files.normalized_artist_name),
normalized_title = COALESCE(
    NULLIF(TRIM(EXCLUDED.normalized_title), ''),
    library_files.normalized_title),
artist_name = COALESCE(
    NULLIF(TRIM(EXCLUDED.artist_name), ''),
    library_files.artist_name)
```

**Policy for hash-changed files:** When a file's content changes (different hash), `work_id` is
reset to NULL. The grouping service re-evaluates on the next pass. The normalized fields use
COALESCE to preserve existing values when the new scan doesn't supply them (e.g., if
`extract_tags()` fails to read artist name but the file was previously populated).

### 2.5 Data backfill (in migration 0011, SQL only)

```sql
-- Backfill work_id for already-enriched files:
UPDATE library_files lf
SET work_id = r.work_id
FROM recordings r
WHERE lf.recording_id = r.id
  AND r.work_id IS NOT NULL;

-- Backfill artists.normalized_name:
UPDATE artists SET normalized_name = lower(trim(name))
WHERE normalized_name IS NULL;
```

**No Python migration.** The `raw_metadata` JSONB backfill (populating `artist_name`,
`normalized_artist_name`, `normalized_title` for existing files) is handled by a **background
maintenance task**, not a blocking migration. See Section 2.6.

### 2.6 Background maintenance task: normalize-backfill

A one-shot Huey task (`normalize_backfill_task`) that runs after app startup when the migration
has been applied but normalized fields are still NULL:

```
SELECT id, raw_metadata FROM library_files
WHERE normalized_artist_name IS NULL
  AND raw_metadata IS NOT NULL
LIMIT 500  -- process in batches
```

For each batch:
1. Extract artist_name, track_title from raw_metadata (reuse extract_tags patterns)
2. UPDATE library_files SET artist_name, normalized_artist_name, normalized_title
3. Run grouping_service for files that now have normalized fields but no work_id

- Batches of 500, each in its own short transaction (no long-running lock).
- Progress tracked via `progress_tracking` table.
- Idempotent: skips files where `normalized_artist_name IS NOT NULL`.
- Files without `raw_metadata` stay NULL — they get populated on next scan.
- The system is fully functional during backfill: files with NULL normalized fields are
  invisible to Step 3 matching but self-heal as the task processes them.

**Partial failure behavior:** Files with NULL normalized fields are invisible to Step 3 matching
(the WHERE clause requires non-null `normalized_artist_name`
and non-null `work_id`). On next scan, the scan service populates the normalized fields and
runs grouping, so they self-heal.

### 2.6 Tables unchanged

`song_masters`, `format_overrides`, `recordings`, `matches`, `log_artists`, `log_identities`,
`log_events` — no schema changes.

---

## 3. Matching Flow (Core Algorithm)

Runs for library file scans only (v1). Artist-first, local-only.

Both `scan_directory` (bulk scan) and `scan_folder_smart` (watcher) code paths must call the
grouping service after writing/upserting a `LibraryFile`. The grouping service is the single
entry point for work assignment.

```
incoming(artist_name, title, file_hash?, recording_mbid?)
  |
  +-- Step 1: HASH SHORTCUT (file scan only)
  |     SELECT work_id FROM library_files
  |     WHERE file_hash = ? AND work_id IS NOT NULL
  |     LIMIT 1
  |     -> Found? Inherit work_id. DONE.
  |
  +-- Step 2: MBID SHORTCUT (if recording_mbid present)
  |     SELECT work_id FROM recordings WHERE id = ?
  |     -> Found work? Inherit. DONE.
  |
  +-- Step 3: ARTIST-FIRST LOCAL MATCH
  |     norm_artist = normalize_artist(artist_name)
  |     norm_title  = normalize_title(title)
  |
  |     -- Group by work_id to avoid redundant fuzzy matches against multiple
  |     -- files in the same work. An artist with 100 files across 5 works
  |     -- only runs rapidfuzz 5 times, not 100.
  |     candidates = SELECT work_id, MIN(normalized_title) AS sample_title
  |                  FROM library_files
  |                  WHERE normalized_artist_name = norm_artist
  |                    AND work_id IS NOT NULL
  |                    AND normalized_title IS NOT NULL
  |                    AND normalized_title != ''
  |                  GROUP BY work_id
  |                  ORDER BY work_id     -- deterministic ordering
  |                  LIMIT 100            -- 100 unique works per artist is plenty
  |
  |     For each candidate:
  |       full    = rapidfuzz.fuzz.ratio(norm_title, candidate.sample_title)
  |       partial = rapidfuzz.fuzz.partial_ratio(norm_title, candidate.sample_title)
  |       score   = 0.7 * full + 0.3 * partial
  |
  |       -- Strict equality override
  |       IF strict_normalize(norm_title) == strict_normalize(candidate.sample_title):
  |         score = 100
  |
  |     best = max(candidates, key=(score, work_id))  -- highest score, then work_id for ties
  |     threshold = dynamic_threshold(len(norm_title))
  |
  |     best.score >= threshold?
  |     -> YES: Inherit candidate's work_id. DONE.
  |     -> NO:  Fall through.
  |
  +-- Step 4: CREATE LOCAL WORK
        -- Advisory lock prevents duplicate works from concurrent scans of the same song.
        -- Key = hash of (norm_artist, norm_title) — same song → same lock.
        lock_key = hashtext(norm_artist || '::' || norm_title)
        SELECT pg_advisory_xact_lock(lock_key)

        -- Re-check Step 3: another thread may have created the work while we waited.
        (re-run the GROUP BY work_id query from Step 3)
        IF match found now: inherit work_id. DONE.

        -- Still no match — safe to create.
        -- Find or create artist (unique index serializes concurrent access):
        INSERT INTO artists(id, name, sort_name, normalized_name, origin,
                            needs_enhancement)
        VALUES (gen_uuid(), raw_artist_name, raw_artist_name, norm_artist,
                'local', FALSE)
        ON CONFLICT (normalized_name) DO NOTHING
        RETURNING id
        -- If RETURNING is empty, SELECT to get the concurrent winner's id.

        work = INSERT INTO works(id, title, artist_id, origin, needs_enhancement)
               VALUES (gen_uuid(), title, artist.id, 'local', FALSE)
        INSERT INTO song_masters(id, work_id, preferred_file_id, selection_method)
               VALUES (gen_uuid(), work.id, file.id, 'auto')
        file.work_id = work.id. DONE.
        -- Advisory lock released on COMMIT.
```

### 3.1 Dynamic thresholds

| Normalized title length | Minimum score |
|-------------------------|---------------|
| < 5 characters          | 95            |
| 5 - 9 characters        | 90            |
| 10 - 25 characters      | 85            |
| > 25 characters         | 80            |

### 3.2 Scoring details

- **Composite:** `0.7 * fuzz.ratio + 0.3 * fuzz.partial_ratio`
- **Strict override:** If both titles, after stripping all non-alphanumeric characters and
  collapsing whitespace, are identical -> score = 100.
- **Deterministic tie-breaking:** Candidates evaluated by `(score DESC, work_id ASC)`. Highest
  score wins. On tie, lowest `work_id` (lexicographic on TEXT) wins. This is **stable and
  deterministic**.
- **Year-only titles preserved:** The normalization guard (Section 4.2) ensures year-only
  titles like "1999" are NOT stripped to empty. They normalize to "1999" and match normally.

### 3.3 Candidate strategy: GROUP BY work_id

Step 3 groups candidates by `work_id` and takes `MIN(normalized_title)` as the representative
title. This means an artist with 100 files across 5 works only runs rapidfuzz 5 times, not 100.
The `LIMIT 100` caps at 100 distinct works per artist — generous for any radio library.

**Why MIN works:** After normalization, version annotations in parentheses are stripped, so
"Hey Jude", "Hey Jude (Remastered)", and "Hey Jude (Radio Edit)" all normalize to "hey jude".
MIN picks one consistently, and they're all equivalent post-normalization.

**Observability:** Log a warning when the LIMIT is hit so production evidence can guide tuning.

### 3.4 Concurrency

Parallel scans or scan+enrichment can race on artist/work creation.

- **Artist creation:** The unique index on `artists.normalized_name` acts as a serialization
  point. `INSERT ON CONFLICT (normalized_name) DO NOTHING` + retry-SELECT guarantees exactly
  one artist per normalized name, regardless of concurrency.
- **Work creation:** No unique constraint on works (legitimate duplicate titles exist). Instead,
  Step 4 acquires a **PostgreSQL advisory lock** keyed on `hashtext(norm_artist || '::' ||
  norm_title)` before creating a work. This forces concurrent threads processing the same song
  to serialize: the second thread re-checks Step 3 after acquiring the lock, finds the work
  created by the first thread, and inherits it instead of creating a duplicate. The lock is
  transaction-scoped (`pg_advisory_xact_lock`) and auto-releases on COMMIT.
- **Enrichment merge:** See Section 5.3.

### 3.5 Trade-off summary

| Step               | Performance                            | Data Integrity                        |
|--------------------|----------------------------------------|---------------------------------------|
| Hash shortcut      | O(1) index lookup (new index)          | Perfect: same file bytes              |
| MBID shortcut      | O(1) index lookup                      | High: authoritative MBID              |
| Artist-first local | O(1) artist + O(min(w, 100)) fuzzy (w=distinct works) | Medium: name collisions possible |
| Create local       | O(1) INSERT with ON CONFLICT           | Always correct; risk of later merging |

---

## 4. Normalization Improvements

### 4.1 Current state (`backend/services/normalization.py`)

Already implements: accent stripping (NFKD), parenthetical annotation removal, feat stripping,
punctuation normalization, lowercasing, leading article removal, whitespace collapse, special
char substitution (& -> and).

**Gap:** NFKD decomposition handles accented Latin characters (e -> e) but does NOT handle
cross-script transliteration (Cyrillic "АС/ДС" vs Latin "AC/DC"). In diverse music libraries,
tags may be in different scripts for the same artist.

### 4.2 Additions

**Transliteration via unidecode** (extend normalization pipeline):
```python
from unidecode import unidecode

# Add to normalize_artist() and normalize_title(), BEFORE lowercasing:
# Transliterates any script to ASCII: "Мötley Crüe" -> "Motley Crue",
# "АС/ДС" -> "AS/DS" (which then matches "AC/DC" after punctuation normalization)
text = unidecode(text)
```

This replaces the existing NFKD + accent stripping step with a more robust transliteration
that handles Cyrillic, CJK, Greek, and other scripts. `unidecode` is a well-established
library (pure Python, no external dependencies). Add `unidecode>=1.3` to `pyproject.toml`.

**Strict normalization function** (new):
```python
def strict_normalize(text: str) -> str:
    """Strip ALL non-alphanumeric chars, collapse whitespace.
    Used as a high-confidence tiebreaker (score = 100 if equal)."""
    base = normalize_title(text)
    stripped = re.sub(r'[^a-z0-9 ]', ' ', base)
    return re.sub(r' +', ' ', stripped).strip()
```

**Year removal outside parentheses** (extend existing):
```python
YEAR_PATTERN = re.compile(r'\b(19[5-9]\d|20[0-2]\d)\b')

# Remove years: "Song 2024" -> "Song"
temp_text = YEAR_PATTERN.sub("", text)
# Only apply year removal if it doesn't strip the entire title
if temp_text.strip() or not text.strip():
    text = temp_text
```

**Guard:** If stripping years would empty the title (e.g., "1999" by Prince), the original
title is preserved. This means "1999" normalizes to "1999" — two files both titled "1999" by
the same artist will correctly match each other. "Hey Jude 2011" still normalizes to
"Hey Jude" as intended.

### 4.3 What was cut from the old plan

| Feature                     | Reason for cutting                              |
|-----------------------------|--------------------------------------------------|
| Stylistic chars ($->s, !->i)| Too rare. Use global_mapping_rules if it occurs. |
| Written-number conversion   | Edge case. Handle via manual merge.              |
| "Pt" expansion (Pt->Part)   | Ultra edge case.                                 |
| Truncation marker removal   | Already handled by partial_ratio scoring.         |
| Artist acronym equivalence  | Too complex for the value.                       |

---

## 5. Non-blocking MB Enrichment

### 5.1 Separation of concerns

**Grouping** (synchronous, during scan):
- Runs Steps 1-4 from Section 3.
- Zero network calls. All local.
- Result: every scanned file has a `work_id`.

**Enrichment** (asynchronous, background task):
- Runs after scan completes.
- Calls MusicBrainz API for files that have MBID tags.
- Updates `works.mbid`, `artists.mbid`, enriches metadata.

### 5.2 Enrichment scenarios

1. **File has `recording_mbid`:** Lookup recording -> get work MBID -> check if `works.mbid`
   already exists:
   - If no existing work with that MBID: update current work's mbid and origin.
   - If existing work found: **merge** local work into canonical work (Section 5.3).

2. **File has `release_mbid` only:** Lookup release -> find recording -> same as (1).

3. **File has no MBIDs:** Skip. Work stays local. Future: AcoustID fingerprinting.

### 5.3 Automatic merge on MBID collision (works)

When enrichment discovers that a local work should be the same as an existing canonical work:

```
-- Acquire row lock to serialize concurrent enrichment merges for the same MBID:
target = SELECT * FROM works WHERE mbid = ? FOR UPDATE

IF target IS NULL:
  -- No collision: just update the local work with MBID data
  UPDATE works SET mbid = ?, origin = 'musicbrainz',
                   needs_enhancement = TRUE
  WHERE id = local_work_id
  RETURN

-- Collision: merge local work into canonical work
-- All in one transaction:
1. local_artist_id = (SELECT artist_id FROM works WHERE id = local_work_id)
2. UPDATE library_files SET work_id = target.id WHERE work_id = local_work_id
3. UPDATE format_overrides SET work_id = target.id
   WHERE work_id = local_work_id
   AND (work_id, format_name) NOT IN (SELECT work_id, format_name FROM format_overrides
                                       WHERE work_id = target.id)
4. DELETE format_overrides WHERE work_id = local_work_id
5. UPDATE recordings SET work_id = target.id WHERE work_id = local_work_id
6. DELETE song_masters WHERE work_id = local_work_id
7. DELETE works WHERE id = local_work_id
8. Recalculate SongMaster for target
9. Orphan cleanup: if local_artist_id has origin='local' and no remaining works/files, DELETE
COMMIT
```

The `SELECT FOR UPDATE` on the target work serializes concurrent enrichment jobs that discover
the same MBID, preventing duplicate canonical works or FK violations.

**Transaction boundary:** Steps 1-7 (including SongMaster recalculation) MUST be in one
transaction. Recalculating SongMaster outside the transaction can transiently violate invariant
#2 (work with files but no SongMaster).

### 5.4 Artist enrichment and merge

Artists have the same local-vs-canonical collision problem as works. When enrichment discovers
`artist_mbid = X`:

```
-- Check if a canonical artist with that MBID already exists:
canonical = SELECT * FROM artists WHERE mbid = X FOR UPDATE

IF canonical IS NULL:
  -- Check if a local artist with matching normalized_name exists:
  local = SELECT * FROM artists WHERE normalized_name = normalize_artist(mb_artist_name)

  IF local IS NOT NULL:
    -- Promote local artist to MB: update in place
    UPDATE artists SET mbid = X, origin = 'musicbrainz',
                       name = mb_name, sort_name = mb_sort_name,
                       disambiguation = mb_disambiguation,
                       needs_enhancement = TRUE
    WHERE id = local.id
    RETURN local.id

  -- No local or canonical match: create new MB artist
  INSERT INTO artists(id, name, sort_name, normalized_name, mbid, origin,
                      needs_enhancement)
  VALUES (gen_uuid(), mb_name, mb_sort_name, normalize_artist(mb_name),
          X, 'musicbrainz', TRUE)
  ON CONFLICT (normalized_name) DO UPDATE SET
    mbid = EXCLUDED.mbid, origin = 'musicbrainz',
    name = EXCLUDED.name, sort_name = EXCLUDED.sort_name,
    needs_enhancement = TRUE
  RETURNING id

-- canonical IS NOT NULL: reuse existing canonical artist
RETURN canonical.id
```

### 5.5 Enrichment service surgery

The current `library_enrichment_service.py` calls:
```python
artist_repo.upsert(Artist(id=artist_mbid, name=..., sort_name=...))
work_repo.upsert(Work(id=work_mbid, title=..., artist_id=artist_mbid))
```

This pattern breaks because `id` is no longer the MBID. The enrichment service must be
refactored to:

1. **Artists:** Replace `artist_repo.upsert()` with `artist_repo.upsert_from_mb(mbid, name,
   sort_name, disambiguation)` — a new method that does the lookup-or-create-or-promote logic
   from Section 5.4. Returns the artist's `id` (which may be a local UUID if an existing local
   artist was promoted).

2. **Works:** Replace `work_repo.upsert()` with `work_repo.upsert_from_mb(mbid, title,
   artist_id)` — does the collision check from Section 5.3, returns the work's `id`.

3. **Recordings:** `recording_repo.upsert()` remains unchanged (recordings already use MBIDs
   as their `id` and are purely MB entities).

4. **`library_files.work_id` consistency:** After any `upsert_from_mb` / merge operation that
   changes which `works.id` a file belongs to, the enrichment service must explicitly update
   `library_files.work_id` to match the resolved work. Today enrichment focuses on
   `update_recording_link()` — the new code must also call `update_work_id()` so the direct
   FK stays in sync with the `recording -> work` chain.

### 5.6 Repository contract changes

| Repository | Old method | New method | Behavior |
|------------|-----------|------------|----------|
| `ArtistRepository` | `upsert(Artist)` | `upsert_from_mb(mbid, name, sort_name, disambig)` | Lookup by mbid or normalized_name, promote/create/reuse. Returns `id`. |
| `ArtistRepository` | — | `upsert_local(name, normalized_name)` | INSERT ON CONFLICT (normalized_name) DO NOTHING + retry-SELECT. Returns `id`. |
| `WorkRepository` | `upsert(Work)` | `upsert_from_mb(mbid, title, artist_id)` | Collision check with FOR UPDATE, merge or create. Returns `id`. |
| `WorkRepository` | — | `create_local(title, artist_id)` | Simple INSERT. Returns `id`. |

The old `upsert()` methods are **deprecated and removed** — there is no ambiguous middle ground.
Enrichment uses `*_from_mb()`. Grouping uses `*_local()`. Both paths are explicit.

### 5.7 Trade-offs

| Aspect                        | Performance                     | Data Integrity                     |
|-------------------------------|----------------------------------|------------------------------------|
| Deferred enrichment           | Scan faster (no API calls)       | Temporary duplicates possible      |
| Auto-merge on MBID collision  | Extra FK update + row lock       | Prevents permanent duplicates      |
| SELECT FOR UPDATE             | Brief serialization point        | Correct under concurrency          |
| Artist promote-in-place       | Single UPDATE vs delete+recreate | Preserves local artist's id (no FK cascade needed) |
| No-MBID files stay local      | Zero API overhead                | No external validation             |

---

## 6. User Actions

Three operations for manual group management. All operate in a single DB transaction.

### 6.1 Merge works

```
POST /api/v1/works/{target_id}/merge
Body: { source_work_ids: ["id1", "id2"] }
```

**Preconditions:**
- target_id must exist -> 404 if not
- target_id must NOT be in source_work_ids -> 422
- Sources that no longer exist are treated as **no-ops** (already merged or deleted).
  This makes the endpoint idempotent — safe to retry after partial failure or double-click.

**Steps (single transaction):**
1. Collect `artist_ids` from source works (for orphan check in step 8)
2. `UPDATE library_files SET work_id = target WHERE work_id IN (sources)`
3. `UPDATE format_overrides SET work_id = target WHERE work_id IN (sources)
    AND (work_id, format_name) NOT IN (SELECT ... WHERE work_id = target)`
   Then `DELETE format_overrides WHERE work_id IN (sources)` (remaining conflicts)
4. `UPDATE recordings SET work_id = target WHERE work_id IN (sources)`
5. `UPDATE matches SET target_id = target
    WHERE target_id IN (sources) AND target_type = 'WORK'`
   (Scoped to `target_type = 'WORK'` to avoid corrupting artist/recording matches)
6. `DELETE song_masters WHERE work_id IN (sources)`
7. `DELETE works WHERE id IN (sources)`
8. Recalculate SongMaster for target
9. **Orphan cleanup:** For each artist_id collected in step 1, check if it still has any
   works or library files. If `origin = 'local'` and zero references remain, DELETE the artist.
   (MB-origin artists are kept even if orphaned — they are canonical entities.)

**Responses:**
- 200: `{ merged_file_count: N, deleted_work_count: N, dropped_override_count: N }`
- 404: `{ error: "work_not_found", id: "target_id" }`  (only for target)
- 422: `{ error: "target_in_sources" }`

**Dropped format_overrides:** Source overrides that conflict with existing target overrides are
silently dropped. The response includes `dropped_override_count` so the caller knows.

### 6.2 Split file from work

```
POST /api/v1/works/{work_id}/split
Body: { file_id: "uuid" }
```

**Preconditions:**
- work_id must exist -> 404
- file_id must exist and belong to work_id -> 422 if not

**Steps:**
1. Create new local work (title from file's `track_title`, artist from file's work's artist,
   `needs_enhancement = FALSE`)
2. `UPDATE library_files SET work_id = new_work WHERE id = file_id`
3. `INSERT song_masters(work_id=new_work, preferred_file_id=file_id, method='auto')`
4. If old work has no remaining files -> `DELETE song_masters` + `DELETE works` for old work
5. If old work not empty and its SongMaster pointed to the split file -> recalculate SongMaster

**Responses:**
- 200: `{ new_work_id: "...", old_work_deleted: bool }`
- 404: `{ error: "work_not_found" }`
- 422: `{ error: "file_not_in_work" }`

### 6.3 Reassign file to different work

```
PATCH /api/v1/library/files/{file_id}/work
Body: { work_id: "target_work_id" }
```

**Preconditions:**
- file_id must exist -> 404
- target_work_id must exist -> 404
- file must not already belong to target -> 422 `{ error: "file_already_in_work" }`
  (true no-op: current `work_id` equals `target_work_id`)

**Steps:**
1. `old_work = file.work_id`
2. `UPDATE library_files SET work_id = target WHERE id = file_id`
3. If old_work now has no files -> DELETE old_work + its song_master
4. Recalculate SongMaster for target (and old_work if not deleted)

**Responses:**
- 200: `{ old_work_id: "...", old_work_deleted: bool }`
- 404: `{ error: "file_not_found" | "work_not_found" }`
- 422: `{ error: "file_already_in_work" }`

### 6.4 Invariants (post-operation assertions)

After any merge/split/reassign/enrichment-merge, the following must hold:
1. No `library_files.work_id` references a non-existent work
2. Every work with at least one file has exactly one `song_masters` entry
3. No `format_overrides.work_id` references a non-existent work
4. No `recordings.work_id` references a non-existent work
5. No `matches` with `target_type = 'WORK'` references a non-existent work
6. No local-origin artist exists with zero works and zero library files (orphan-free)

These are enforced by FK constraints and the explicit cleanup steps. Integration tests must
assert all five after each operation.

---

## 7. Critical Files to Modify

| File | Change |
|------|--------|
| `backend/db/migrations/0011_local_grouping.sql` | Schema changes + SQL backfill (work_id, normalized_name) |
| `backend/tasks/normalize_backfill_task.py` | **NEW**: Background task to backfill normalized fields from raw_metadata |
| `backend/domain/models.py` | Add `mbid`, `origin`, `normalized_name` to Artist/Work. Add fields to LibraryFile. |
| `backend/domain/enums.py` | Add `Origin` enum: LOCAL, MUSICBRAINZ |
| `backend/services/normalization.py` | Add `strict_normalize()`, `unidecode` transliteration, extend year removal |
| `pyproject.toml` | Add `unidecode>=1.3` dependency |
| `backend/services/grouping_service.py` | **NEW**: Steps 1-4 matching algorithm |
| `backend/services/library_scan_service.py` | Populate `artist_name`, `normalized_*` during scan. Call grouping_service. |
| `backend/services/library_enrichment_service.py` | Replace upsert calls with `upsert_from_mb()`. Add MBID-collision merge. |
| `backend/repositories/artists.py` | Replace `upsert()` with `upsert_local()` + `upsert_from_mb()`. Add `get_by_normalized_name()`. |
| `backend/repositories/works.py` | Replace `upsert()` with `create_local()` + `upsert_from_mb()`. Add `get_by_mbid()`, `merge_into()`, `delete_if_empty()`. |
| `backend/repositories/library_files.py` | Add `get_candidates_by_artist()`, `update_work_id()` |
| `backend/db/repositories/pg_artists.py` | Implement new methods |
| `backend/db/repositories/pg_works.py` | Implement new methods |
| `backend/db/repositories/pg_library_files.py` | Implement new methods. Fix ON CONFLICT to preserve `work_id`. |
| `backend/routers/library.py` | Add merge/split/reassign endpoints. Update Pydantic response models with `mbid`, `origin`. |
| `backend/tasks/library_tasks.py` | Integrate grouping_service into scan pipeline |
| `backend/tasks/library_watcher_tasks.py` | Ensure watcher scan path calls grouping_service |
| `frontend/src/lib/schemas/artists.ts` | Add `mbid`, `origin` fields |
| `frontend/src/lib/schemas/works.ts` | Add `mbid`, `origin` fields |

---

## 8. Verification

### 8.1 Unit tests — fixed corpora

`tests/test_grouping_service.py` with deterministic test data:

**Threshold boundary tests:**
- Title "Hi" (2 chars, threshold=95): score 94 -> no match, score 95 -> match
- Title "Hello" (5 chars, threshold=90): score 89 -> no match, score 90 -> match
- Title "Hey Jude" (8 chars, threshold=90): exact match -> 100
- Title "Bohemian Rhapsody" (17 chars, threshold=85): score 84 -> no match

**Scoring tests:**
- Identical titles -> score 100 (strict override)
- "Start-Me-Up" vs "Start Me Up" -> score 100 (strict normalize match)
- "Hey Jude" vs "Hey Jude (Remastered)" -> high score via partial_ratio
- "Hey Jude" vs "Let It Be" -> low score, no match
- "Go" vs "Go!" -> strict normalize catches it (both -> "go")

**Year stripping regression guard:**
- "Hey Jude 2011" vs "Hey Jude" -> match (year stripped, titles identical)

**Year-only title guard:**
- Title "1999" normalizes to "1999" (year removal guard preserves it)
- Two files titled "1999" by same artist -> same work (correct match)

**Tie-breaking:**
- Two candidates with equal score -> lowest id wins (deterministic)

**Concurrency:**
- Two threads creating the same normalized artist -> ON CONFLICT, both get same artist id

### 8.2 Unit tests — normalization

- `strict_normalize("Start-Me-Up")` -> `"start me up"`
- `strict_normalize("Don't Stop")` -> `"dont stop"`
- Year removal: `"Hey Jude 2011"` -> `"Hey Jude"`
- Year-only title preserved: `"1999"` -> `"1999"` (guard prevents empty result)

### 8.3 Unit tests — upsert preservation

- Upsert unchanged file (same hash) -> `work_id` preserved
- Upsert modified file (different hash) -> `work_id` reset to NULL
- Upsert with NULL artist_name when existing has value -> COALESCE preserves existing

### 8.4 Integration tests

- Scan directory with mixed MBID/no-MBID files -> all files get work_ids
- Scan duplicate files (same hash, different paths) -> same work_id
- Scan "Hey Jude.mp3" then "Hey Jude (Remastered).mp3" by same artist -> same work
- Scan "Hey Jude.mp3" then "Let It Be.mp3" by same artist -> different works
- Re-scan unchanged file -> work_id preserved
- MB enrichment: local artist promoted to MB-origin when MBID discovered
- MB enrichment merge: local work + canonical work with same MBID -> single work
- Concurrent enrichment: two jobs discover same MBID -> no duplicates
- Merge API: verify all 5 invariants from Section 6.4
- Split API: verify invariants + old work cleanup
- Reassign API: verify invariants + old work cleanup
- Watcher scan: new file discovered via watcher -> gets work_id via grouping service
- Partial migration: files with NULL normalized fields invisible to matching, self-heal on scan

### 8.5 Invariant assertions (reusable test helper)

```python
def assert_grouping_invariants(conn):
    """Run after every merge/split/reassign/enrichment test."""
    # 1. No dangling library_files.work_id
    assert 0 == count("library_files lf LEFT JOIN works w ON lf.work_id = w.id
                        WHERE lf.work_id IS NOT NULL AND w.id IS NULL")
    # 2. Every work with files has exactly one song_master
    assert 0 == count("works w JOIN library_files lf ON lf.work_id = w.id
                        LEFT JOIN song_masters sm ON sm.work_id = w.id
                        WHERE sm.id IS NULL GROUP BY w.id")
    # 3. No dangling format_overrides
    assert 0 == count("format_overrides fo LEFT JOIN works w ON fo.work_id = w.id
                        WHERE w.id IS NULL")
    # 4. No dangling recordings.work_id
    assert 0 == count("recordings r LEFT JOIN works w ON r.work_id = w.id
                        WHERE r.work_id IS NOT NULL AND w.id IS NULL")
    # 5. No dangling matches targeting deleted works
    assert 0 == count("matches m LEFT JOIN works w ON m.target_id = w.id
                        WHERE m.target_type = 'WORK' AND w.id IS NULL")
    # 6. No orphaned local artists
    assert 0 == count("artists a
                        LEFT JOIN works w ON w.artist_id = a.id
                        LEFT JOIN library_files lf ON lf.normalized_artist_name = a.normalized_name
                        WHERE a.origin = 'local'
                          AND w.id IS NULL AND lf.id IS NULL")
```

### 8.6 Automated E2E test (`tests/test_grouping_e2e.py`)

Full pipeline integration test with real DB and fixture audio files. Marked
`@pytest.mark.integration`.

```python
@pytest.mark.integration
class TestGroupingE2E:
    """End-to-end grouping pipeline: scan -> group -> enrich -> merge -> split."""

    def test_scan_assigns_work_ids(self, test_audio_dir, db_conn):
        """Scan a directory with 5 fixture files -> all get work_ids."""
        # Fixtures: 3 files by "Test Artist" (2 versions of "Song A", 1 "Song B")
        #           2 files by "Other Artist" ("Song C" x2)
        scan_result = library_scan_service.scan_directory(test_audio_dir, ...)
        files = library_file_repo.get_by_folder_path(test_audio_dir)
        assert all(f.work_id is not None for f in files)
        # "Song A" and "Song A (Remastered)" share a work
        song_a_files = [f for f in files if "Song A" in f.track_title]
        assert song_a_files[0].work_id == song_a_files[1].work_id
        # "Song B" has its own work
        song_b = [f for f in files if "Song B" in f.track_title][0]
        assert song_b.work_id != song_a_files[0].work_id
        assert_grouping_invariants(db_conn)

    def test_rescan_preserves_work_ids(self, test_audio_dir, db_conn):
        """Re-scan unchanged files -> work_ids preserved."""
        library_scan_service.scan_directory(test_audio_dir, ...)
        first_scan = {f.file_path: f.work_id
                      for f in library_file_repo.get_by_folder_path(test_audio_dir)}
        library_scan_service.scan_directory(test_audio_dir, ...)
        second_scan = {f.file_path: f.work_id
                       for f in library_file_repo.get_by_folder_path(test_audio_dir)}
        assert first_scan == second_scan
        assert_grouping_invariants(db_conn)

    def test_merge_works(self, test_audio_dir, db_conn, test_client):
        """Merge two works -> files consolidated, invariants hold."""
        library_scan_service.scan_directory(test_audio_dir, ...)
        works = work_repo.get_by_artist(artist_id)
        target, source = works[0], works[1]
        resp = test_client.post(f"/api/v1/works/{target.id}/merge",
                                json={"source_work_ids": [source.id]})
        assert resp.status_code == 200
        assert resp.json()["deleted_work_count"] == 1
        # Source work no longer exists
        assert work_repo.get_by_id(source.id) is None
        # All files point to target
        files = library_file_repo.get_candidates_by_artist(norm_artist)
        assert all(f.work_id == target.id for f in files)
        assert_grouping_invariants(db_conn)

    def test_split_file_from_work(self, test_audio_dir, db_conn, test_client):
        """Split a file out -> new work created, old work intact."""
        library_scan_service.scan_directory(test_audio_dir, ...)
        work = work_repo.get_by_artist(artist_id)[0]
        files = [f for f in library_file_repo.get_by_folder_path(test_audio_dir)
                 if f.work_id == work.id]
        assert len(files) >= 2  # need at least 2 files to split
        resp = test_client.post(f"/api/v1/works/{work.id}/split",
                                json={"file_id": str(files[1].id)})
        assert resp.status_code == 200
        new_work_id = resp.json()["new_work_id"]
        assert new_work_id != work.id
        # Split file now in new work
        split_file = library_file_repo.get_by_id(files[1].id)
        assert split_file.work_id == new_work_id
        # Original file still in original work
        orig_file = library_file_repo.get_by_id(files[0].id)
        assert orig_file.work_id == work.id
        assert_grouping_invariants(db_conn)

    def test_reassign_file(self, test_audio_dir, db_conn, test_client):
        """Reassign a file to a different work."""
        library_scan_service.scan_directory(test_audio_dir, ...)
        works = work_repo.get_by_artist(artist_id)
        file_to_move = [f for f in library_file_repo.get_by_folder_path(test_audio_dir)
                        if f.work_id == works[0].id][0]
        resp = test_client.patch(f"/api/v1/library/files/{file_to_move.id}/work",
                                 json={"work_id": works[1].id})
        assert resp.status_code == 200
        moved = library_file_repo.get_by_id(file_to_move.id)
        assert moved.work_id == works[1].id
        assert_grouping_invariants(db_conn)

    def test_normalize_backfill_task(self, db_conn):
        """Backfill task populates NULL normalized fields and assigns work_ids."""
        # Insert a file with raw_metadata but NULL normalized fields
        # (simulates post-migration state)
        insert_file_with_null_normalized(db_conn, raw_metadata={...})
        normalize_backfill_task()
        file = library_file_repo.get_by_id(file_id)
        assert file.normalized_artist_name is not None
        assert file.normalized_title is not None
        assert file.work_id is not None
        assert_grouping_invariants(db_conn)

    def test_watcher_scan_groups_new_file(self, test_audio_dir, db_conn):
        """Watcher detects new file -> grouping service assigns work_id."""
        # Initial scan
        library_scan_service.scan_directory(test_audio_dir, ...)
        # Add new file to directory (same artist, same song)
        copy_fixture("Song A (Live).mp3", test_audio_dir)
        # Watcher scan
        scan_folder_smart(test_audio_dir, ...)
        new_file = library_file_repo.get_by_path(
            os.path.join(test_audio_dir, "Song A (Live).mp3"))
        assert new_file.work_id is not None
        # Should join existing "Song A" work
        song_a_work = [f for f in library_file_repo.get_by_folder_path(test_audio_dir)
                       if "Song A" in (f.track_title or "")][0].work_id
        assert new_file.work_id == song_a_work
        assert_grouping_invariants(db_conn)
```

Test fixture audio files live in `tests/fixtures/audio/` — small valid audio files with
known tags (created via mutagen in conftest.py or committed as tiny binary fixtures).
