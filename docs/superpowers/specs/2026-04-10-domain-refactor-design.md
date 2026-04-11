# Domain Refactor Design

**Date:** 2026-04-10  
**Scope:** `backend/domain/` — enums, models, repository interfaces  
**Blast radius:** 104 Python files (48 test, 56 backend) + 5 TypeScript files in `frontend/`

---

## Context

`backend/domain/` has accumulated four categories of problems that make the domain hard to read and reason about:

1. The `Log*` prefix is overloaded — it means "broadcast/playlist log entry" on three models and "application logging" on three others. New readers and AI assistants consistently confuse them.
2. `Artist.id`, `Work.id`, and `Recording.id` carry a `# MBID` comment that is factually wrong and obscures an intentional asymmetry between the three entities.
3. Enum string values use two inconsistent casings (UPPERCASE and lowercase), with a third PascalCase convention on `TargetType`. This causes silent bugs when enum values are compared to DB strings.
4. All 20+ dataclasses live in one 266-line `models.py` file, making subdomain intent invisible.

---

## Issue 1 — Rename `Log*` Broadcast Models

### Rename map

| Current | Proposed | Meaning |
|---|---|---|
| `LogArtist` | `BroadcastArtist` | Artist name as it appears in an ingested broadcast playlist |
| `LogIdentity` | `TrackIdentity` | Unique artist+title signature extracted from a playlist |
| `LogEvent` | `PlayEvent` | Single play occurrence tied to a playlist and time |

`BroadcastArtist` (not `PlaylistArtist`) — avoids the reading "an artist who creates playlists"; emphasises the producer-side broadcast origin.

### Field rename

`TrackIdentity.artist_id` → `TrackIdentity.broadcast_artist_id`

This field references `BroadcastArtist.id` (broadcast-layer UUID), not catalog `Artist.id`. The rename eliminates a persistent FK ambiguity. **20 callsites** across backend + tests.

### File cascade (30+ files across 6 layers)

| Layer | Action |
|---|---|
| `backend/domain/models.py` | Rename 3 dataclasses + field rename |
| `backend/repositories/` | `log_artists.py` → `broadcast_artists.py`, `log_identities.py` → `track_identities.py`, `log_events.py` → `play_events.py`; rename ABC classes inside |
| `backend/db/repositories/` | Same file renames; rename `PgLogArtistRepository` → `PgBroadcastArtistRepository`, `PgLogIdentityRepository` → `PgTrackIdentityRepository`, `PgLogEventRepository` → `PgPlayEventRepository` |
| `tests/fakes/` | Same file renames; rename `FakeBroadcastArtistRepository` etc. |
| `backend/services/repository_factory.py` | Rename attributes `self.log_artists` → `self.broadcast_artists`, `self.log_identities` → `self.track_identities`, `self.log_events` → `self.play_events`; update all `repos.log_*` access |
| Services / tasks / tests (~21 files) | Update import lines + `repos.log_*` attribute access |

**Parameter naming policy:** Rename all function parameters `log_artist_repo` → `broadcast_artist_repo`, `log_identity_repo` → `track_identity_repo`, `log_event_repo` → `play_event_repo`. Consistency across the API surface.

### Repository interface class renames

| Current | Proposed |
|---|---|
| `LogArtistRepository` | `BroadcastArtistRepository` |
| `LogIdentityRepository` | `TrackIdentityRepository` |
| `LogEventRepository` | `PlayEventRepository` |

### Integration test and conftest files

`tests/conftest.py` hardcodes table names for cleanup/truncation. `tests/integration/test_migrations.py` may reference table names directly. Both must be updated as part of Step 1:
- `log_artists` → `broadcast_artists`
- `log_identities` → `track_identities`
- `log_events` → `play_events`

### DB migration: `0014_rename_log_tables.sql`

Use `ALTER TABLE ... RENAME TO` — PostgreSQL automatically updates FK constraints that reference the renamed table, but named indexes and column names do **not** auto-rename.

**Table renames:**
- `log_artists` → `broadcast_artists`
- `log_identities` → `track_identities`
- `log_events` → `play_events`

**Column rename:**
- `track_identities.artist_id` → `track_identities.broadcast_artist_id`

**Index renames (7 explicit `ALTER INDEX ... RENAME TO`):**

| Current | Renamed to |
|---|---|
| `idx_log_artists_embedding` | `idx_broadcast_artists_embedding` |
| `idx_log_identities_embedding` | `idx_track_identities_embedding` |
| `idx_log_identities_artist` | `idx_track_identities_broadcast_artist` |
| `idx_log_identities_status` | `idx_track_identities_status` |
| `idx_log_events_playlist` | `idx_play_events_playlist` |
| `idx_log_events_identity` | `idx_play_events_identity` |
| `idx_log_events_played_at` | `idx_play_events_played_at` |

**Note on `matches` table:** Has FK columns referencing both `log_artists` and `log_identities`. FK cascade covers both automatically. Run a targeted test after migration to confirm match queries still resolve correctly.

**FK constraint names** on `matches` will carry the old names after the table rename (PostgreSQL renames the table but not the constraint name). Add explicit `ALTER TABLE matches RENAME CONSTRAINT` statements if constraint naming hygiene matters.

---

## Issue 2 — Replace Misleading `# MBID` Comments

`Artist.id`, `Work.id`, and `Recording.id` all carried `# MBID` comments but the three entities use three **different ID strategies** — an intentional asymmetry the comments were obscuring:

```python
@dataclass
class Artist:
    id: str  # local UUID (str); MusicBrainz ID lives in the separate `mbid` field
    ...
    mbid: str | None = None

@dataclass
class Work:
    id: str  # local UUID (str); MusicBrainz ID lives in the separate `mbid` field
    ...
    mbid: str | None = None

@dataclass
class Recording:
    id: str  # MusicBrainz recording MBID used directly as PK (no separate mbid field)
```

`Recording` differs from `Artist`/`Work` — its MusicBrainz MBID *is* the primary key. A future type migration (`str` → `UUID` across all three, plus ABC params and FK fields) is tracked as a separate follow-up issue.

---

## Issue 3 — Normalize Enum Value Casing to Lowercase

### Enums to change — complete member lists

`LogLevel` is **excluded** from normalization (see note below).

**`MatchStatus`** (all 6 members):
`"PENDING"` → `"pending"`, `"AUTO_MATCHED"` → `"auto_matched"`, `"NEEDS_REVIEW"` → `"needs_review"`, `"MANUAL_MATCHED"` → `"manual_matched"`, `"AUTO_REJECTED"` → `"auto_rejected"`, `"MANUAL_REJECTED"` → `"manual_rejected"`

**`MatchTier`** (all 6 members — two renames, not just case changes):

| Member | Old name | Old value | New name | New value |
|---|---|---|---|---|
| — | `MBID_EXACT` | `"MBID_EXACT"` | `MUSICBRAINZ_ID_EXACT` | `"musicbrainz_id_exact"` |
| — | `NORMALIZATION` | `"NORMALIZATION"` | `NORMALIZATION` | `"normalization"` |
| — | `VECTOR` | `"VECTOR"` | `VECTOR` | `"vector"` |
| — | `MUSICBRAINZ_API` | `"MUSICBRAINZ_API"` | `MUSICBRAINZ_API` | `"musicbrainz_api"` |
| — | `MANUAL` | `"MANUAL"` | `MANUAL` | `"manual"` |
| — | `UNKNOWN` | `"UNKNOWN"` | `UNCLASSIFIED` | `"unclassified"` |

`MBID_EXACT` → `MUSICBRAINZ_ID_EXACT`: removes abbreviation inconsistency (`mbid` vs spelled-out `musicbrainz`). `UNKNOWN` → `UNCLASSIFIED`: `UNKNOWN` is a sentinel assigned only when all matching tiers are exhausted; `UNCLASSIFIED` signals this meaning explicitly.

**`FileStatus`** (all 3 members):
`"PRESENT"` → `"present"`, `"MISSING"` → `"missing"`, `"DELETED"` → `"deleted"`

**`VersionType`** (all 17 members):
`"ORIGINAL"` → `"original"`, `"LIVE"` → `"live"`, `"REMASTER"` → `"remaster"`, `"REMIX"` → `"remix"`, `"RADIO_EDIT"` → `"radio_edit"`, `"DEMO"` → `"demo"`, `"ACOUSTIC"` → `"acoustic"`, `"EXTENDED"` → `"extended"`, `"INSTRUMENTAL"` → `"instrumental"`, `"EXPLICIT"` → `"explicit"`, `"CLEAN"` → `"clean"`, `"COVER"` → `"cover"`, `"EDITION"` → `"edition"`, `"ALTERNATE"` → `"alternate"`, `"FORMAT"` → `"format"`, `"UNKNOWN"` → `"unknown"`, `"OTHER"` → `"other"`

Note: `VersionType` values are home-grown heuristics from `normalization.py`'s `classify_version_descriptor()` — they are **not** sourced from MusicBrainz. `EXPLICIT`/`CLEAN` are RIAA content descriptors rather than true version types; extracting them into a separate enum is tracked as a follow-up.

**`TargetType`** (all 4 members):
`"Artist"` → `"artist"`, `"Work"` → `"work"`, `"Recording"` → `"recording"`, `"LibraryFile"` → `"library_file"`

**Already lowercase — no change:** `EnrichmentStatus`, `ReleaseType`, `ReleaseStatus`, `SelectionMethod`, `Origin`, `TaskType`, `TaskStatus`, `LogCategory`.

### `LogLevel` exception

`LogLevel` stays UPPERCASE. Add comment:

```python
# uppercase intentional: matches Python logging, structlog, and external sink conventions
class LogLevel(StrEnum):
    DEBUG   = "DEBUG"
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"
```

### `TargetType` shape-change note

`"LibraryFile"` → `"library_file"` is a shape change, not just a case fold. Any raw string comparison to `"LibraryFile"` — in DB queries, serialised JSON, or API responses — will silently break if the code change and migration are not applied atomically. The `target_type` column exists in both `matches` and `global_mapping_rules` — both must be in the same migration pass.

### Full surface area sweep (pre-commit checklist)

Enum values are serialised via `.value` on `StrEnum`. Beyond Python source, verify all of the following — many will silently break if missed.

**1. Known raw-string callsites in Python:**

| File | Lines | Raw string | Action |
|---|---|---|---|
| `backend/db/repositories/library_files.py` | 55 | `FileStatus(row.get("file_status", "PRESENT"))` | Change fallback to `"present"` |
| `backend/routers/library.py` | 526, 629, 1240, 1368 | `"ORIGINAL"` (version_type fallback) | Change to `"original"` |
| `tests/routers/test_matching.py` | 173, 182, 305, 353, 389, 422 | `"NEEDS_REVIEW"`, `"PENDING"`, `"AUTO_MATCHED"`, `"LibraryFile"` | Update to lowercase |
| `tests/routers/test_playlists.py` | 234 | `"PENDING"` | → `"pending"` |
| `tests/domain/test_enums.py` | 10, 13, 16 | `== "PRESENT"`, `== "MISSING"`, `== "DELETED"` | Update to lowercase |
| All 5 `MatchTier.MBID_EXACT` callsites | identity_matching_service.py ×3, test_identity_matching.py ×2 | Symbol rename | → `MatchTier.MUSICBRAINZ_ID_EXACT` |
| All 4 `MatchTier.UNKNOWN` callsites | identity_matching_service.py, fakes, tests | Symbol rename | → `MatchTier.UNCLASSIFIED` |

The `library_files.py` fallback is the highest-risk point: if the stored DB value is lowercase but the fallback string is still `"PRESENT"`, any row missing the column raises `ValueError` at runtime.

**Also grep raw SQL string literals** (not just Python symbols) — routers contain table names in raw SQL strings that a symbol grep will miss:
```
grep -rn '"PENDING"\|"PRESENT"\|"ORIGINAL"\|"MBID_EXACT"\|"UNKNOWN"\|"LibraryFile"\|"MANUAL_MATCHED"' \
  --include="*.py" --include="*.ts" .
```

**2. TypeScript frontend — Zod schemas and display maps (must update atomically with backend):**

| File | Content | Action |
|---|---|---|
| `frontend/src/lib/schemas/matcher.ts` | `ArtistResolutionSchema` and `IdentityResolutionSchema` use `z.enum(['MANUAL_MATCHED', 'MANUAL_REJECTED'])` — **request payloads** | → `['manual_matched', 'manual_rejected']` |
| `frontend/src/pages/matcher/MatcherBrowser.tsx` line 48 | `'MANUAL_MATCHED'` | → `'manual_matched'` |
| `frontend/src/lib/schemas/library.ts` lines 3–18 | `LibraryFile`/`LibraryStatus` schema definitions | Update all uppercase literals |
| `frontend/src/components/domain/works/VersionBadges.tsx` lines 2–17 | `VERSION_LABELS` map keyed by uppercase `VersionType` values; filters exclude `UNKNOWN`, `OTHER` | Re-key map to lowercase; update `UNKNOWN` filter to `unclassified` |
| `frontend/src/components/domain/matcher/SearchSlideOver.tsx` | `LibraryFile` type interface | → `"library_file"` |

The Zod schema files are request-payload validators — if not updated atomically, the frontend sends uppercase values the backend rejects (silent 422s).

**3. JSON/JSONB blob fields** — inspect write paths before committing:
- `broadcast_artists.artist_candidates` — dict blobs written by ingestion
- `library_files.raw_metadata` — audio tag dump; may include `release_type`, `release_status` strings
- `progress_tracking.progress_data` — task progress payloads; may embed `TaskType`/`TaskStatus` strings
- `system_logs.details` — arbitrary audit dict; check log callsites for embedded enum strings

**4. Task/Huey serialization:** Confirmed safe — `huey.db` configured with `results=False`; tasks are fire-and-forget with no result persistence.

**5. Seed / fixture SQL:** No `seed.sql` or fixture SQL files found — confirmed clean.

**6. Scripts directory:** `scripts/leann_mcp_server.py`, `scripts/leann_reindex.py`, `scripts/start.sh`, `scripts/start.ps1` — grep for hardcoded enum strings before closing Step 3.

### DB migration: `0015_lowercase_enum_values.sql`

New migration file — do **not** edit historical files. Wrapped in a transaction; by the time this runs, `0014` has already executed (use new table names).

```sql
BEGIN;

UPDATE broadcast_artists SET match_status = LOWER(match_status);
ALTER TABLE broadcast_artists ALTER COLUMN match_status SET DEFAULT 'pending';

UPDATE track_identities SET match_status = LOWER(match_status);
ALTER TABLE track_identities ALTER COLUMN match_status SET DEFAULT 'pending';

UPDATE matches SET match_tier = CASE
    WHEN match_tier = 'MBID_EXACT'      THEN 'musicbrainz_id_exact'
    WHEN match_tier = 'NORMALIZATION'   THEN 'normalization'
    WHEN match_tier = 'VECTOR'          THEN 'vector'
    WHEN match_tier = 'MUSICBRAINZ_API' THEN 'musicbrainz_api'
    WHEN match_tier = 'MANUAL'          THEN 'manual'
    WHEN match_tier = 'UNKNOWN'         THEN 'unclassified'
    ELSE LOWER(match_tier) END;
ALTER TABLE matches ALTER COLUMN match_tier SET DEFAULT 'unclassified';

UPDATE matches SET target_type = CASE
    WHEN target_type = 'LibraryFile' THEN 'library_file'
    ELSE LOWER(target_type) END;

UPDATE library_files SET file_status = LOWER(file_status);
ALTER TABLE library_files ALTER COLUMN file_status SET DEFAULT 'present';

-- recordings has unique index on (work_id, version_type) — UPDATE is safe, no new uniqueness conflicts
UPDATE recordings SET version_type = LOWER(version_type);
ALTER TABLE recordings ALTER COLUMN version_type SET DEFAULT 'original';

UPDATE global_mapping_rules SET target_type = CASE
    WHEN target_type = 'LibraryFile' THEN 'library_file'
    ELSE LOWER(target_type) END;

UPDATE system_logs SET level = LOWER(level);

COMMIT;
```

A companion **`0015_rollback_lowercase_enum_values.sql`** must be written with the inverse (UPPER + case-specific CASE statements for `musicbrainz_id_exact` → `MBID_EXACT`, `unclassified` → `UNKNOWN`).

`0004_library_layer.sql` already uses `'pending'` for `enrichment_status` — no change needed there.

### DB objects inventory

Confirmed clean: migrations `0001`–`0013` contain no `CREATE VIEW`, `CREATE MATERIALIZED VIEW`, `CREATE FUNCTION`, `CREATE TRIGGER`, or `CREATE POLICY` statements.

---

## Issue 4 — Minor Renames (before split)

Perform these before splitting `models.py` so the new subdomain files are born with correct names.

| Current | Proposed | Affected files | DB impact |
|---|---|---|---|
| `GlobalMappingRule` | `MappingRule` | 21 files | Table rename: `global_mapping_rules` → `mapping_rules` |
| `Origin` | `CatalogSource` | ~15 files | Python-only — `origin` column values (`"local"`, `"musicbrainz"`) already lowercase, no migration |

**`MappingRule` documentation:** Add a **class-level docstring** (not module-level — module docstrings are skipped by IDE hover and `help()`):

```python
@dataclass
class MappingRule:
    """A system-wide pattern-matching override applied before tiered matching.

    Rules are evaluated against normalized_name (artist pipeline) and
    normalized_signature (identity pipeline) across all stations and playlists.
    Priority is descending — first match wins.

    No station-scoped or playlist-scoped rules exist. If they are introduced,
    this class should be renamed SystemMappingRule to provide a contrast point.
    """
```

**`Origin` lives in `enums.py`**, not `models.py`. The rename happens in `enums.py`; `catalog.py` imports `CatalogSource` from `enums` for field type annotations.

**`MusicBrainzCache` keeps its current name** — `Mb` is an abbreviation requiring domain knowledge.

**`GlobalMappingRule` fake rename:** `tests/fakes/global_mapping_rules.py` → `tests/fakes/mapping_rules.py`; rename `FakeGlobalMappingRuleRepository` → `FakeMappingRuleRepository` inside.

**`RepositoryFactory` attribute:** `self.global_mapping_rules` → `self.mapping_rules` in `backend/services/repository_factory.py`.

**`Origin` → `CatalogSource` grep gate:** Use qualified patterns to avoid false positives: `from backend.domain`, `: Origin`, `Origin =`, `origin: Origin`.

### DB migration: `0016_rename_mapping_tables.sql`

**Pre-condition check (run before the migration):** Verify no other tables have FK constraints pointing at `global_mapping_rules`:

```sql
SELECT conname, conrelid::regclass
FROM pg_constraint
WHERE confrelid = 'global_mapping_rules'::regclass;
```

If the query returns rows, those FK constraints must be dropped and re-added within the same transaction as the rename. If it returns zero rows (expected — no cross-table FKs exist in the current schema), proceed directly.

```sql
ALTER TABLE global_mapping_rules RENAME TO mapping_rules;
ALTER INDEX idx_rules_priority RENAME TO idx_mapping_rules_priority;
```

Update `tests/conftest.py` and `tests/integration/test_migrations.py` table name references.

---

## Issue 5 — Split `models.py` into Subdomain Files + Architecture Improvements

### Subdomain layout

Models move from `models.py`; enums stay in `enums.py`. **`enums.py` stays as one unified module** throughout — no split, to avoid circular import risk.

| New file | Models | Notes |
|---|---|---|
| `backend/domain/broadcast.py` | `Station`, `Playlist`, `BroadcastDay`, `BroadcastArtist`, `TrackIdentity`, `PlayEvent` | `PlayEvent` gets `frozen=True` |
| `backend/domain/library.py` | `AudioMetadata`, `LibraryFile`, `LibraryQuarantine`, `LibraryFolder` | — |
| `backend/domain/catalog.py` | `Artist`, `Work`, `Recording` | Imports `CatalogSource` from `enums.py` |
| `backend/domain/matching.py` | `Match`, `MappingRule` | `Match` gets `__post_init__` guard; module docstring added |
| `backend/domain/curation.py` | `SongMaster`, `FormatOverride` | — |
| `backend/domain/system.py` | `MusicBrainzCache`, `TaskProgress`, `UserSetting`, `SystemLog` | `MusicBrainzCache` gets `frozen=True`; module comment added |

### Architecture improvements bundled into the split

**`PlayEvent` — `frozen=True`:** A broadcast event that already occurred is an immutable historical record. Confirmed: no post-creation field mutations anywhere in the codebase.

**`MusicBrainzCache` — `frozen=True`:** A write-once API snapshot. Confirmed: `cache_repo.set()` always creates new instances.

**`Match` — `__post_init__` XOR guard:** The `matches` DB table enforces `(identity_id IS NOT NULL AND artist_id IS NULL) OR (identity_id IS NULL AND artist_id IS NOT NULL)` via a CHECK constraint. The Python domain model currently accepts any combination. Add:

```python
def __post_init__(self) -> None:
    has_identity = self.identity_id is not None
    has_artist = self.artist_id is not None
    if has_identity == has_artist:
        raise ValueError(
            "Match must have exactly one of identity_id or artist_id set; "
            f"got identity_id={self.identity_id!r}, artist_id={self.artist_id!r}"
        )
```

**`system.py` module comment:**
```python
# LogLevel and LogCategory enums (in enums.py) belong to this subdomain.
# They are application-logging concerns, not broadcast-log models.
# The Log* prefix here refers to observability — not playlist ingestion.
```

### Transition strategy

1. Create the 6 subdomain files.
2. Replace `models.py` with an **explicit-import shim** (no `*` to avoid leaking stdlib names):

```python
# backend/domain/models.py  — TEMPORARY SHIM, delete once all import sites updated
from backend.domain.broadcast import (
    BroadcastArtist, BroadcastDay, PlayEvent, Playlist, Station, TrackIdentity,
)
from backend.domain.catalog import Artist, Recording, Work
from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.library import AudioMetadata, LibraryFile, LibraryFolder, LibraryQuarantine
from backend.domain.matching import Match, MappingRule
from backend.domain.system import MusicBrainzCache, SystemLog, TaskProgress, UserSetting
```

Note: `CatalogSource` is an enum — consumers import from `backend.domain.enums`, not from `models.py`. No shim entry needed.

3. Update import sites one subdomain at a time (6 batches), running tests + typecheck after each batch.
4. Delete `models.py` once the shim is empty.

**`backend/domain/__init__.py` stays empty** — the shim lives in `models.py` to preserve the existing import pattern during transition.

---

## Execution Order

| Step | Change | Migration file | Safety gate before commit |
|---|---|---|---|
| 0 | Confirm baseline — all existing tests pass | — | `uv run pytest -m "not integration and not slow"` green |
| 1 | Rename `Log*` models → `BroadcastArtist`/`TrackIdentity`/`PlayEvent` + field rename + repo/Pg/fake/factory files + integration test table lists + DB tables/column/indexes | `0014_rename_log_tables.sql` | Grep (Python symbols + raw SQL literals) returns zero hits for `LogArtist`, `LogIdentity`, `LogEvent`, `log_artists`, `log_identities`, `log_events`, `artist_id` on `TrackIdentity`; tests green; `mypy + ruff` clean |
| 2 | Replace `# MBID` comments with accurate documentation | — | Visual review only |
| 3 | Lowercase 5 enums (excl. `LogLevel`) + `MatchTier` renames (`MBID_EXACT`→`MUSICBRAINZ_ID_EXACT`, `UNKNOWN`→`UNCLASSIFIED`) + patch all Python + TS callsites + write rollback script | `0015_lowercase_enum_values.sql` + rollback | **Deployment:** stop app (`Ctrl+C` → `start.ps1`), apply migration, restart. Grep (Python + TS) returns zero hits for all old uppercase strings + old `MBID_EXACT`/`UNKNOWN` symbols; frontend build passes; tests green; `mypy + ruff` clean |
| 4 | Rename `GlobalMappingRule`→`MappingRule`, `Origin`→`CatalogSource` | `0016_rename_mapping_tables.sql` | Qualified grep returns zero hits for `GlobalMappingRule`, `: Origin`, `Origin =`; conftest + integration tests updated; tests green; `mypy + ruff` clean |
| 5 | Split `models.py` into 6 subdomain files + `frozen=True` + `Match.__post_init__` + `system.py` comment + explicit-import shim | — | All tests green; `tests/domain/test_match_invariant.py` exists with three cases (neither set raises, both set raises, valid construction passes); shim contains only domain model names (no stdlib leakage); `mypy + ruff` clean |
| 6 | Update 104 import sites (one subdomain batch at a time) | — | After **each batch**: tests green + `uv run mypy backend --strict` + `uv run ruff check .` |
| 7 | Delete `models.py` shim | — | `uv run pytest` (full suite incl. integration) green; grep for `from backend.domain.models` returns zero |

---

## Release Verification

Step 3 is a **breaking API change** — enum values in JSON responses change casing. Verification before shipping:

- **Schema diff:** Run a before/after diff of API response shapes for endpoints returning `match_status`, `match_tier`, `file_status`, `version_type`, and `target_type` fields.
- **Frontend smoke test:** Confirm matcher browser, version badges, and library schema render correctly after TS updates.
- **Zod validation:** Confirm `ArtistResolutionSchema` and `IdentityResolutionSchema` accept lowercase values and the backend accepts those payloads.
- **Observability:** Any dashboards or alerts filtering on old enum strings (`target_type = 'LibraryFile'`, `level = 'ERROR'`) will silently stop matching. Update queries before deploying.

---

## Post-Merge Follow-Ups (non-blocking for CI)

- Update design docs under `docs/` that reference `global_mapping_rules` or old model/table names.
- `Artist/Work/Recording.id: str` → `UUID` type migration: requires 3-ABC, 3-Pg-impl, 3-fake cascade + SQL parameter rebind; own plan.
- `Match` rename to `IdentityMatch`/`ArtistMatch` split: deferred; revisit after `matching.py` shape is visible.
- `VersionType` RIAA rating extraction (`EXPLICIT`/`CLEAN`): home-grown classifier; open separate issue.
- `LogCategory`/`TaskType` hidden coupling: document invariant or unify to `TaskCategory`; defer.
- `system.py` split into `infrastructure.py` + `settings.py`: defer; module docstring covers intent for now.

---

## Out of Scope

- `enhancement` vs `enrichment` terminology: **intentionally distinct lifecycles.** Enhancement = binary MusicBrainz metadata gate on catalog entities (`needs_enhancement` bool). Enrichment = multi-stage library file pipeline (`EnrichmentStatus`: PENDING → CATEGORIZED → ENRICHED → FAILED/SKIPPED). Do not align them.
- `enums.py` split: out of scope to avoid circular import risk. All enums stay in `enums.py`.
- `backend/domain/__init__.py` remains empty throughout.
