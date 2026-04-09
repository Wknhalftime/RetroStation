# RetroStation — System Design Specification

**Date:** 2026-03-31
**Status:** Approved — ready for implementation planning
**Approach:** Thin vertical slice, 42 bounded sessions

---

## 1. System Purpose

RetroStation reconstructs historical radio playlists by matching non-canonical broadcast logs (CSV exports from radio station playout systems) against a local audio file library and authoritative MusicBrainz metadata.

**The core loop:**
1. Import a messy, inconsistent CSV radio log
2. Normalize and deduplicate the artist/title strings it contains
3. Match each unique identity against local audio files — automatically where confident, surfacing ambiguous cases to the curator
4. Curator resolves ambiguous matches via a web dashboard
5. Export M3U playlists for playback in Navidrome

**This is a single-user personal tool.** No authentication, no multi-user, no roles. The "curator" is Lance.

---

## 2. Architecture Overview

### 2.1 Process Model

Three processes managed by `honcho` reading a `Procfile`. PostgreSQL 16 is an external Windows service dependency — must be running before honcho starts. No retry logic; a clear startup error is sufficient.

```
# Procfile
api:    uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
worker: uv run python -m huey.bin.huey_consumer backend.tasks.huey_app.huey -w 1
web:    cd frontend && npm run dev
```

**Critical Windows constraints:**
- Do NOT use `--reload` on uvicorn — causes silent 500 errors on Windows via watchfiles
- `logging_config.py` must call `sys.stdout.reconfigure(encoding='utf-8')` and `sys.stderr.reconfigure(encoding='utf-8')` before any other logging setup — prevents Unicode crashes on cp1252 Windows consoles
- All file paths use `pathlib.Path` throughout — never hardcode backslashes

### 2.2 Two Independent Pipelines

Both pipelines must run before matching produces meaningful results. They are independent and can run in any order, but the recommended first-time setup sequence is: library scan → library enrichment → import CSV → matching runs automatically.

```
Pipeline A — Radio Log                  Pipeline B — Library
──────────────────────                  ────────────────────
POST /api/v1/ingestion/playlists        POST /api/v1/library/scan
        │                                       │
        ▼                                       ▼
ingestion_task                          library_scan_task
(CSV → log_artists/identities/events)   (mutagen → library_files)
        │                                       │
        ▼                                       ▼
embedding_task                          library_enrichment_task
(BGE-M3 vectors)                        (MB API → recordings/works/artists)
        │                                       │
        ▼                                       ▼
artist_matching_task                    mb_enrichment_tasks
        │                               (fills needs_enhancement=TRUE)
        ▼
identity_matching_task
        │
        └─────────────────┬──────────────────────┘
                          ▼
                   matches table
              (log_identity → library_file)
                          │
                          ▼
              master_selection_service
              (song_masters auto-population)
```

If CSVs are imported before the library is scanned, all identities surface as NEEDS_REVIEW. `POST /api/v1/matching/run` re-runs the pipeline on all non-manually-resolved identities after the library is ready.

### 2.3 Design Principles

- **Library-file as match target.** MusicBrainz data is lookup infrastructure, never the resolution target. A log entry is "matched" when it resolves to a `library_files` row.
- **Artist-first gating.** Title matching is blocked until the `log_artist` is resolved. If `log_artist.match_status = AUTO_REJECTED`, all child `log_identities` immediately become `AUTO_REJECTED` without running any title tiers.
- **Identity deduplication.** 50,000 play events collapse to ~1,500 unique `log_identities`. The pipeline processes identities, not events.
- **Thin routers → service layer → repository layer.** Routers contain only request parsing and auth. Business logic lives in `services/`. All SQL lives in `db/repositories/`.
- **FakeRepository for testing.** No `unittest.mock`. Repository ABCs have in-memory dict-backed fakes in `tests/fakes/`. Services depend only on ABCs.

### 2.4 Task Chaining Rule

**Never call `.get()` on a Huey task from within a running Huey task.** With `-w 1` (single worker thread), this deadlocks. All task chaining uses fire-and-forget `.enqueue()` at the end of each task. Database writes within tasks use direct `psycopg` calls, not sub-tasks.

---

## 3. Data Model

Nine migration files in `backend/db/migrations/`. The migration runner creates `schema_migrations` before applying any numbered migration. All other tables are created exclusively by their numbered migration file.

### 3.1 Migration Index

| File | Description |
|------|-------------|
| `0001_observation_layer.sql` | `playlists`, `log_artists`, `log_identities`, `log_events` |
| `0002_canonical_layer.sql` | `artists`, `works`, `recordings` |
| `0003_matching_layer.sql` | `matches`, `global_mapping_rules` |
| `0004_library_layer.sql` | `library_files`, `library_quarantine` |
| `0005_vector_indexes.sql` | `CREATE EXTENSION vector`, `ALTER TABLE` adds `embedding vector(1024)` to 4 tables, HNSW indexes |
| `0006_settings_and_ops.sql` | `user_settings`, `system_logs`, `progress_tracking` |
| `0007_stations.sql` | `stations`, `broadcast_days`, `ALTER TABLE playlists ADD COLUMN station_id`, `ALTER TABLE log_events ADD COLUMN broadcast_day_id` |
| `0008_song_masters.sql` | `song_masters`, `format_overrides` |
| `0009_mb_cache.sql` | `mb_cache` |

> Full DDL for all migrations is in **Section 3.5**. The migration index above is a navigation aid only.

### 3.2 Key Tables and Relationships

**Observation layer (radio log data — never edited after ingestion):**
- `stations` — call_letters (NOT NULL UNIQUE), name, city, format_name. Created explicitly by user before CSV import. `format_name` is the join key to `format_overrides`. Note: call_letters includes the suffix (e.g. `KAZR-FM`); the UI should be aware that `KAZR-FM` and `KAZR` refer to the same station but are stored as entered.
- `playlists` — id, station_id, name, contenet_hash, ingested_at. One row per CSV file. `content_hash` (SHA-256) is the deduplication guard; uploading the same CSV twice returns 409.
- `log_artists` — deduplicated by `normalized_name`. Carries `match_status`, `embedding vector(1024)`.
- `log_identities` — deduplicated by `normalized_signature` (MD5 of `normalize_artist||normalize_title`). Carries `match_status`, `match_tier`, `embedding vector(1024)`.
- `log_events` — one row per CSV line. Unique on `(identity_id, playlist_id, played_at)`.
- `broadcast_days` — one row per calendar date per station; enables the Playlist Viewer date picker.

**Canonical layer (populated by library enrichment pipeline):**
- `artists` — MusicBrainz canonical. PK is MBID (TEXT).
- `works` — a musical composition (abstract). FK to `artists`.
- `recordings` — a specific audio version of a work. FK to `works`. Carries `version_type`, `embedding vector(1024)`.

**Library layer (populated by filesystem scan):**
- `library_files` — one row per audio file. Absolute path, SHA-256 hash, extracted tags (all MBID columns, release_title, track_title, format, bitrate, duration_ms, release_type, release_status). FK to `recordings` (nullable until enrichment runs). Full tag dump in `raw_metadata JSONB`.
- `library_quarantine` — files that failed mutagen extraction.

**Resolution layer:**
- `matches` — core join: `log_identity_id → library_file_id`. XOR constraint: exactly one of `identity_id` or `artist_id` is non-null. Carries `confidence_score`, `match_tier`.
- `song_masters` — one preferred `library_file_id` per `work_id`. `selection_method` is `'auto'` or `'manual'`. Manual selections survive rescans.
- `format_overrides` — per-station-format file preference. Takes priority over `song_masters` during M3U generation.
- `global_mapping_rules` — curator-defined regex/exact rules applied before any matching tier.

**Infrastructure:**
- `user_settings` — key/value store for paths, thresholds, Navidrome path mapping.
- `progress_tracking` — one row per running/recent background task. WebSocket broadcast loop polls this every 500ms.
- `mb_cache` — caches MusicBrainz API responses. Prevents redundant API calls.

### 3.3 Upsert Semantics (Critical)

All three deduplicated tables use `ON CONFLICT DO NOTHING`. When conflict fires, the existing row keeps its current `match_status` — **never reset** on re-import:

```sql
-- log_artists: dedup by normalized_name
INSERT INTO log_artists (id, original_name, normalized_name, match_status)
VALUES (:id, :name, :normalized, 'PENDING')
ON CONFLICT (normalized_name) DO NOTHING;
-- Then SELECT id WHERE normalized_name = :normalized to get the id regardless

-- log_identities: dedup by normalized_signature
INSERT INTO log_identities (..., normalized_signature, match_status)
VALUES (..., :sig, 'PENDING')
ON CONFLICT (normalized_signature) DO NOTHING;

-- log_events: dedup by (identity_id, playlist_id, played_at)
INSERT INTO log_events (...) VALUES (...)
ON CONFLICT (identity_id, playlist_id, played_at) DO NOTHING;
```

### 3.4 M3U Resolution Priority Chain

```sql
-- Priority: format_override → song_master → direct match
SELECT COALESCE(fo.preferred_file_id, sm.preferred_file_id, m.library_file_id)
FROM log_identities li
JOIN matches m ON m.identity_id = li.id
LEFT JOIN library_files lf ON lf.id = m.library_file_id
LEFT JOIN recordings rec ON rec.id = lf.recording_id
LEFT JOIN song_masters sm ON sm.work_id = rec.work_id
LEFT JOIN format_overrides fo ON fo.work_id = rec.work_id
    AND fo.format_name = :station_format_name
WHERE li.id = :identity_id
ORDER BY m.confidence_score DESC LIMIT 1;
```

---

### 3.5 Complete Table DDL

Canonical source of truth for all column names, types, constraints, and indexes.
Organized by migration file. **Never invent column names — consult this section first.**

> **Cross-migration dependency notes:**
> - `embedding vector(1024)` columns in `log_artists`, `log_identities`, `works`, `recordings` — added via `ALTER TABLE` in **0005** (requires `CREATE EXTENSION vector` first).
> - `matches.library_file_id` FK references `library_files` (created in 0004) — column added without FK in **0003**, constraint added via `ALTER TABLE` in **0004**.
> - `playlists.station_id` FK references `stations` (created in 0007) — added via `ALTER TABLE` in **0007**.
> - `log_events.broadcast_day_id` FK references `broadcast_days` (created in 0007) — added via `ALTER TABLE` in **0007**.

---

#### 0001 — Observation Layer

```sql
CREATE TABLE playlists (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    content_hash TEXT        NOT NULL UNIQUE,  -- SHA-256 of raw CSV bytes
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    -- station_id UUID added in 0007_stations.sql
);

CREATE TABLE log_artists (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    original_name     TEXT        NOT NULL,
    normalized_name   TEXT        NOT NULL UNIQUE,
    match_status      TEXT        NOT NULL DEFAULT 'PENDING',
    artist_candidates JSONB,      -- top candidate matches stored for NEEDS_REVIEW UI
    error_message     TEXT,       -- last matching error, if any
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE TABLE log_identities (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    artist_id            UUID        NOT NULL REFERENCES log_artists(id),
    original_title       TEXT        NOT NULL,
    normalized_title     TEXT        NOT NULL,
    normalized_signature TEXT        NOT NULL UNIQUE,  -- 32-char MD5
    match_status         TEXT        NOT NULL DEFAULT 'PENDING',
    match_tier           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);
CREATE INDEX idx_log_identities_artist ON log_identities(artist_id);
CREATE INDEX idx_log_identities_status ON log_identities(match_status);

CREATE TABLE log_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id UUID        NOT NULL REFERENCES log_identities(id),
    playlist_id UUID        NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    played_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (identity_id, playlist_id, played_at)
    -- broadcast_day_id UUID added in 0007_stations.sql
);
CREATE INDEX idx_log_events_playlist  ON log_events(playlist_id);
CREATE INDEX idx_log_events_identity  ON log_events(identity_id);
CREATE INDEX idx_log_events_played_at ON log_events(played_at);
```

---

#### 0002 — Canonical Layer

```sql
CREATE TABLE artists (
    id                TEXT        PRIMARY KEY,  -- MusicBrainz MBID
    name              TEXT        NOT NULL,
    sort_name         TEXT        NOT NULL,
    disambiguation    TEXT,
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
);

CREATE TABLE works (
    id                TEXT        PRIMARY KEY,  -- MBID
    title             TEXT        NOT NULL,
    artist_id         TEXT        NOT NULL REFERENCES artists(id),
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);
CREATE INDEX idx_works_artist ON works(artist_id);

CREATE TABLE recordings (
    id                TEXT        PRIMARY KEY,  -- MBID
    title             TEXT        NOT NULL,
    work_id           TEXT        REFERENCES works(id),
    duration_ms       INTEGER,
    version_type      TEXT        NOT NULL DEFAULT 'ORIGINAL',
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);
CREATE INDEX idx_recordings_work ON recordings(work_id);
```

---

#### 0003 — Matching Layer

```sql
-- NOTE: library_file_id has no FK here — library_files does not exist until 0004.
-- The FK constraint is added via ALTER TABLE in 0004 after library_files is created.
CREATE TABLE matches (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id      UUID REFERENCES log_identities(id),
    artist_id        UUID REFERENCES log_artists(id),
    library_file_id  UUID,               -- FK added in 0004_library_layer.sql
    target_id        TEXT,               -- MBID or UUID-as-text
    target_type      TEXT,               -- 'Artist'|'Work'|'Recording'|'LibraryFile'
    confidence_score REAL     NOT NULL DEFAULT 0.0,
    match_tier       TEXT     NOT NULL DEFAULT 'UNKNOWN',
    trace_id         TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT xor_match_target CHECK (
        (identity_id IS NOT NULL AND artist_id IS NULL)
        OR (identity_id IS NULL  AND artist_id IS NOT NULL)
    ),
    UNIQUE (identity_id, library_file_id),
    UNIQUE (artist_id, target_id)
);
CREATE INDEX idx_matches_identity     ON matches(identity_id);
CREATE INDEX idx_matches_artist       ON matches(artist_id);
CREATE INDEX idx_matches_library_file ON matches(library_file_id);

CREATE TABLE global_mapping_rules (
    id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    source_pattern TEXT    NOT NULL,
    target_type    TEXT    NOT NULL,  -- 'Artist'|'Work'|'Recording'|'LibraryFile'
    target_id      TEXT    NOT NULL,  -- MBID or library_file UUID as text
    priority       INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_rules_priority ON global_mapping_rules(priority DESC);
```

---

#### 0004 — Library Layer

```sql
CREATE TABLE library_files (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path            TEXT        NOT NULL UNIQUE,
    file_hash            TEXT        NOT NULL,       -- SHA-256 of file content
    indexed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id             TEXT,

    -- Canonical recording link (nullable until enrichment runs)
    recording_id         TEXT        REFERENCES recordings(id),

    -- MusicBrainz IDs from file tags (populated by scanner, no API calls)
    recording_mbid       TEXT,   -- musicbrainz_trackid tag
    artist_mbid          TEXT,   -- musicbrainz_artistid tag       (track-level artist)
    album_artist_mbid    TEXT,   -- musicbrainz_albumartistid tag  (primary album artist)
    release_mbid         TEXT,   -- musicbrainz_albumid tag

    -- Release metadata from tags
    release_title        TEXT,
    release_type         TEXT,            -- ReleaseType enum values
    release_type_secondary TEXT,          -- secondary MB type: 'compilation'|'live'|'remix'|'demo'
    release_status       TEXT,            -- ReleaseStatus enum values; 'promotion' = promo

    -- Track metadata from tags
    track_title          TEXT,
    track_number         SMALLINT,        -- parsed from '6/8' slash notation → 6
    disc_number          SMALLINT,        -- parsed from '1/2' slash notation → 1
    duration_ms          INTEGER,

    -- File format (for master selection scoring)
    format               TEXT        NOT NULL DEFAULT 'unknown',  -- FileFormat values
    bitrate              INTEGER,    -- kbps from mutagen

    -- Enrichment state
    enrichment_status    TEXT        NOT NULL DEFAULT 'pending',  -- EnrichmentStatus values

    -- Full tag dump
    raw_metadata         JSONB
);

-- Partial indexes for enrichment pipeline batching
CREATE INDEX idx_library_files_enrichment_album
    ON library_files(enrichment_status, release_mbid)
    WHERE enrichment_status = 'pending' AND release_mbid IS NOT NULL;

CREATE INDEX idx_library_files_enrichment_recording
    ON library_files(enrichment_status, recording_mbid)
    WHERE enrichment_status = 'pending' AND recording_mbid IS NOT NULL AND release_mbid IS NULL;

-- Artist/album browsing
CREATE INDEX idx_library_files_artist_mbid       ON library_files(artist_mbid);
CREATE INDEX idx_library_files_album_artist_mbid ON library_files(album_artist_mbid);
CREATE INDEX idx_library_files_release_mbid      ON library_files(release_mbid);
CREATE INDEX idx_library_files_recording_id      ON library_files(recording_id);

CREATE TABLE library_quarantine (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path     TEXT        NOT NULL,
    error_message TEXT        NOT NULL,
    trace_id      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deferred FK: now that library_files exists, add the constraint to matches
ALTER TABLE matches
    ADD CONSTRAINT fk_matches_library_file
    FOREIGN KEY (library_file_id) REFERENCES library_files(id);
```

---

#### 0005 — Vector Extension + Embedding Columns

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- Add embedding columns now that the type exists
ALTER TABLE log_artists    ADD COLUMN embedding vector(1024);
ALTER TABLE log_identities ADD COLUMN embedding vector(1024);
ALTER TABLE works          ADD COLUMN embedding vector(1024);
ALTER TABLE recordings     ADD COLUMN embedding vector(1024);

-- HNSW indexes for cosine similarity search
CREATE INDEX idx_log_artists_embedding
    ON log_artists    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);
CREATE INDEX idx_log_identities_embedding
    ON log_identities USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);
CREATE INDEX idx_works_embedding
    ON works          USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);
CREATE INDEX idx_recordings_embedding
    ON recordings     USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);
```

---

#### 0006 — Settings & Operations

```sql
CREATE TABLE user_settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE system_logs (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id   TEXT,
    category   TEXT        NOT NULL,
    level      TEXT        NOT NULL,   -- 'DEBUG'|'INFO'|'WARNING'|'ERROR'
    message    TEXT        NOT NULL,
    details    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_system_logs_created ON system_logs(created_at DESC);
CREATE INDEX idx_system_logs_level   ON system_logs(level);

CREATE TABLE progress_tracking (
    task_id       TEXT        PRIMARY KEY,
    task_type     TEXT        NOT NULL,   -- TaskType enum values
    status        TEXT        NOT NULL DEFAULT 'running',  -- TaskStatus enum values
    progress_data JSONB       NOT NULL DEFAULT '{}',
    -- progress_data shape by task_type:
    --   scan:       { files_done, files_total, current_path }
    --   enrichment: { files_done, albums_done, api_calls, cache_hits }
    --   ingestion:  { rows_done, rows_total, identities_created }
    --   matching:   { identities_done, auto_matched, needs_review, rejected }
    --   m3u_export: { events_done, events_total, skipped }
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);
CREATE INDEX idx_progress_status_time ON progress_tracking(status, updated_at);
CREATE INDEX idx_progress_type_status ON progress_tracking(task_type, status);
CREATE INDEX idx_progress_stale
    ON progress_tracking(updated_at)
    WHERE status = 'running';  -- detects tasks not updated in >10 minutes
```

---

#### 0007 — Stations

```sql
CREATE TABLE stations (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_letters TEXT        NOT NULL UNIQUE,  -- e.g. 'KAZR-FM'
    name         TEXT,
    city         TEXT,
    format_name  TEXT,   -- freeform; join key to format_overrides.format_name
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE broadcast_days (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id     UUID NOT NULL REFERENCES stations(id),
    broadcast_date DATE NOT NULL,
    UNIQUE (station_id, broadcast_date)
);
CREATE INDEX idx_broadcast_days_station ON broadcast_days(station_id);

-- Deferred FKs now that stations and broadcast_days exist
ALTER TABLE playlists   ADD COLUMN station_id      UUID REFERENCES stations(id) ON DELETE SET NULL;
ALTER TABLE log_events  ADD COLUMN broadcast_day_id UUID REFERENCES broadcast_days(id);

CREATE INDEX idx_playlists_station ON playlists(station_id);
```

---

#### 0008 — Song Masters

```sql
CREATE TABLE song_masters (
    id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT    NOT NULL REFERENCES works(id),
    preferred_file_id UUID    NOT NULL REFERENCES library_files(id),
    selection_method  TEXT    NOT NULL DEFAULT 'auto',  -- SelectionMethod enum values
    score             INTEGER,   -- computed score when selection_method='auto'
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_id)
);

CREATE TABLE format_overrides (
    id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT    NOT NULL REFERENCES works(id),
    format_name       TEXT    NOT NULL,   -- must match stations.format_name
    preferred_file_id UUID    NOT NULL REFERENCES library_files(id),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_id, format_name)
);
CREATE INDEX idx_format_overrides_work   ON format_overrides(work_id);
CREATE INDEX idx_format_overrides_format ON format_overrides(format_name);
```

---

#### 0009 — MusicBrainz Cache

```sql
CREATE TABLE mb_cache (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key     TEXT        NOT NULL UNIQUE,
    -- Format: '{entity_type}:{mbid}:{browse_type}:{offset}:{limit}'
    -- e.g. 'artist:b10bbbfc-...:works:0:100'
    entity_type   TEXT        NOT NULL,
    entity_mbid   TEXT        NOT NULL,
    response_data JSONB       NOT NULL,
    cached_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_mb_cache_entity ON mb_cache(entity_type, entity_mbid);
CREATE INDEX idx_mb_cache_expiry ON mb_cache(expires_at);
```

---

## 4. Backend Structure

### 4.1 Enum Reference (`domain/enums.py`)

All status and type values are defined here as `str` enums. Never use raw string literals in services, repositories, or routers — always reference these enums.

```python
class MatchStatus(str, Enum):
    PENDING        = "PENDING"
    AUTO_MATCHED   = "AUTO_MATCHED"
    NEEDS_REVIEW   = "NEEDS_REVIEW"
    MAN_MATCHED    = "MAN_MATCHED"
    AUTO_REJECTED  = "AUTO_REJECTED"
    MAN_REJECTED   = "MAN_REJECTED"

class MatchTier(str, Enum):
    MBID_EXACT      = "MBID_EXACT"
    NORMALIZATION   = "NORMALIZATION"
    VECTOR          = "VECTOR"
    MUSICBRAINZ_API = "MUSICBRAINZ_API"
    MANUAL          = "MANUAL"
    UNKNOWN         = "UNKNOWN"

class TargetType(str, Enum):
    ARTIST       = "Artist"
    WORK         = "Work"
    RECORDING    = "Recording"
    LIBRARY_FILE = "LibraryFile"

class VersionType(str, Enum):
    ORIGINAL   = "ORIGINAL"
    LIVE       = "LIVE"
    REMASTER   = "REMASTER"
    REMIX      = "REMIX"
    RADIO_EDIT = "RADIO_EDIT"
    DEMO       = "DEMO"
    ACOUSTIC   = "ACOUSTIC"
    OTHER      = "OTHER"

class EnrichmentStatus(str, Enum):
    PENDING     = "pending"
    CATEGORIZED = "categorized"
    ENRICHED    = "enriched"
    FAILED      = "failed"
    SKIPPED     = "skipped"

class ReleaseType(str, Enum):
    ALBUM       = "album"
    SINGLE      = "single"
    EP          = "ep"
    COMPILATION = "compilation"
    LIVE        = "live"
    BROADCAST   = "broadcast"
    OTHER       = "other"

class ReleaseStatus(str, Enum):
    OFFICIAL       = "official"
    PROMOTION      = "promotion"     # MusicBrainz "promo" category
    BOOTLEG        = "bootleg"
    PSEUDO_RELEASE = "pseudo-release"

class SelectionMethod(str, Enum):
    AUTO   = "auto"
    MANUAL = "manual"

class TaskType(str, Enum):
    SCAN        = "scan"
    ENRICHMENT  = "enrichment"
    INGESTION   = "ingestion"
    RULES_APPLY = "rules_apply"
    MATCHING    = "matching"
    M3U_EXPORT  = "m3u_export"

class TaskStatus(str, Enum):
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    TIMEOUT   = "timeout"
```

**Column mapping:**

| Enum | DB column | Tables |
|------|-----------|--------|
| `MatchStatus` | `match_status` | `log_artists`, `log_identities` |
| `MatchTier` | `match_tier` | `log_identities`, `matches` |
| `TargetType` | `target_type` | `matches`, `global_mapping_rules` |
| `VersionType` | `version_type` | `recordings` |
| `EnrichmentStatus` | `enrichment_status` | `library_files` |
| `ReleaseType` | `release_type` | `library_files` |
| `ReleaseStatus` | `release_status` | `library_files` |
| `SelectionMethod` | `selection_method` | `song_masters` |
| `TaskType` | `task_type` | `progress_tracking` |
| `TaskStatus` | `status` | `progress_tracking` |

### 4.2 File Structure

```
backend/
├── main.py                          # FastAPI app, lifespan (pool + migrations)
├── config.py                        # pydantic-settings Settings, @lru_cache get_settings()
├── dependencies.py                  # get_db_connection(), get_current_token()
├── logging_config.py                # structlog setup; UTF-8 reconfigure MUST be first
│
├── domain/
│   └── enums.py                     # ALL status/type enums — never raw string literals
│
├── routers/
│   ├── v1.py                        # include_router for all sub-routers under /api/v1
│   ├── stations.py
│   ├── ingestion.py
│   ├── library.py
│   ├── matching.py
│   ├── playlists.py
│   ├── settings.py
│   └── tasks.py                     # GET /api/v1/tasks/active only
│
├── services/
│   ├── repository_factory.py        # RepositoryFactory(conn) — instantiates real repos
│   ├── normalization.py             # normalize_artist(), normalize_title(), compute_signature()
│   ├── ingestion_service.py
│   ├── artist_matching_service.py
│   ├── identity_matching_service.py
│   ├── library_scan_service.py
│   ├── library_enrichment_service.py
│   ├── master_selection_service.py  # Scoring algorithm, song_masters population
│   ├── m3u_generator_service.py     # Priority chain resolution + Navidrome path mapping
│   ├── embedding_service.py         # BGE-M3 singleton — imported ONLY by embedding_tasks.py
│   └── mb_client.py                 # MusicBrainz API, 1.1s rate limit, mb_cache integration
│
├── repositories/                    # ABCs (19 files)
│   ├── stations.py
│   ├── playlists.py
│   ├── broadcast_days.py
│   ├── log_artists.py
│   ├── log_identities.py
│   ├── log_events.py
│   ├── library_files.py
│   ├── library_quarantine.py
│   ├── matches.py
│   ├── artists.py
│   ├── works.py
│   ├── recordings.py
│   ├── song_masters.py
│   ├── format_overrides.py
│   ├── global_mapping_rules.py
│   ├── mb_cache.py
│   ├── progress_tracking.py
│   └── settings.py
│
├── db/
│   ├── pool.py                      # ConnectionPool singleton (lazy=True, opened in lifespan)
│   ├── migrations.py                # Runner: schema_migrations check → apply in order
│   ├── migrations/                  # 0001–0009 SQL files
│   └── repositories/               # PostgreSQL implementations (mirrors repositories/)
│
├── tasks/
│   ├── huey_app.py                  # SqliteHuey(filename='huey.db', results=False)
│   ├── ingestion_tasks.py           # → enqueues embedding_task on completion
│   ├── embedding_tasks.py           # playlist-scoped; → enqueues artist_matching_task
│   ├── artist_matching_tasks.py     # → enqueues identity_matching_task
│   ├── identity_matching_tasks.py   # terminal; triggers master_selection_service
│   ├── library_tasks.py
│   ├── library_enrichment_tasks.py  # links files to canonical entities
│   └── mb_enrichment_tasks.py       # fills metadata on existing canonical entities
│
└── websocket.py                     # /ws endpoint; token via ?token= query param;
                                     # polls progress_tracking every 500ms;
                                     # marks stale tasks failed (>10min no update)
```

**Router registration pattern:**
```python
# backend/routers/v1.py
router = APIRouter(prefix="/api/v1")
router.include_router(stations.router,  prefix="/stations",  tags=["stations"])
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
router.include_router(library.router,   prefix="/library",   tags=["library"])
router.include_router(matching.router,  prefix="/matching",  tags=["matching"])
router.include_router(playlists.router, prefix="/playlists", tags=["playlists"])
router.include_router(settings.router,  prefix="/settings",  tags=["settings"])
router.include_router(tasks.router,     prefix="/tasks",     tags=["tasks"])
# WebSocket at /ws is registered directly on app in main.py — not under /api/v1
```

**Pool initialization contract:** `db/pool.py` creates the pool with `lazy=True`. `main.py` lifespan calls `pool.open()` after migrations run, `pool.close()` on shutdown. The worker process maintains its own independent pool instance — pools are never shared across OS processes.

**`embedding_service.py` is imported only by `embedding_tasks.py`.** The API process never imports `sentence-transformers`. The ~1.1GB BGE-M3 model loads only in the worker process.

---

## 5. Matching Pipeline

### 5.1 Huey Task Chain

```
ingestion_task
  Receive: file bytes + station_id (multipart)
  Check: SHA-256 content_hash → 409 if exists in playlists
  Write: playlists, log_artists (ON CONFLICT DO NOTHING),
         log_identities (ON CONFLICT DO NOTHING), log_events, broadcast_days
  Compute: normalized_signature = MD5(normalize_artist||normalize_title)
  On completion: enqueue embedding_task(playlist_id)

embedding_task
  Scope: only artists/identities linked to THIS playlist_id
         WHERE embedding IS NULL
  Write: vector(1024) to log_artists.embedding, log_identities.embedding
  On completion: enqueue artist_matching_task(playlist_id)

artist_matching_task
  Guard: SELECT ... FOR UPDATE SKIP LOCKED (prevents double-processing across playlists)
  For each log_artist WHERE match_status='PENDING' linked to this playlist:
    0. global_mapping_rules pre-check (exact string then regex, priority DESC, first match wins)
    Tier 1 (exact): normalize(log_artist.original_name) == normalize(artists.name)
    Tier 2 (fuzzy): rapidfuzz.token_sort_ratio() against all artists.name rows
    Tier 3 (MB API): mb_client.search_artist() if Tiers 1–2 produce no confident match
                     → upsert into artists, set needs_enhancement=TRUE
    Thresholds: ≥95 + gap≥mb_score_gap → AUTO_MATCHED
                ≥80 → AUTO_MATCHED (NORMALIZATION)
                60–79 → NEEDS_REVIEW
                <60 → next tier
    Cascade: if artist → AUTO_REJECTED, all child log_identities → AUTO_REJECTED immediately
  On completion: enqueue identity_matching_task(playlist_id)

identity_matching_task
  For each log_identity WHERE log_artist.match_status IN (AUTO_MATCHED, MAN_MATCHED):
    0. global_mapping_rules pre-check
    Tier 1 (MBID exact): skip for standard CSV input (future-proofing)
    Tier 2 (MBID graph): artist MBID confirmed → candidate library_files →
                         rapidfuzz.ratio() on normalized title
                         ≥95 → AUTO_MATCHED/MBID_EXACT
                         80–94 → AUTO_MATCHED/NORMALIZATION
                         60–79 → NEEDS_REVIEW | <60 → Tier 3
    Tier 3 (text): combined score = (artist_score × 0.4) + (title_score × 0.6)
                   ≥90 → AUTO_MATCHED/NORMALIZATION
                   70–89 → NEEDS_REVIEW | <70 → Tier 4
    Tier 4 (vector): pgvector HNSW cosine search on recordings.embedding
                     distance ≤0.15 → NEEDS_REVIEW/VECTOR (never auto-accept)
                     distance >0.15 → AUTO_REJECTED
  Write: matches row, update log_identity.match_status AND match_tier
  Collect newly matched work_ids → trigger master_selection_service.recalculate()
         (only for selection_method='auto' — never overwrite manual selections)
  No further enqueue (terminal task)
```

### 5.2 Score Gap Check

```python
candidates.sort(key=lambda x: x["score"], reverse=True)
top_score = candidates[0]["score"]
second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
gap = top_score - second_score  # infinity if only one candidate

if top_score >= mb_auto_link_score and gap >= mb_score_gap:
    → AUTO_MATCHED
elif top_score >= mb_auto_link_score and gap < mb_score_gap:
    → NEEDS_REVIEW  # ambiguous even though score clears threshold
elif top_score >= 80:
    → AUTO_MATCHED (with gap check still applied)
elif top_score >= 60:
    → NEEDS_REVIEW
else:
    → fall through to next tier
```

### 5.3 Global Mapping Rules

```python
def rule_matches(source_pattern: str, normalized_value: str) -> bool:
    if source_pattern == normalized_value:
        return True  # exact match first
    try:
        return bool(re.fullmatch(source_pattern, normalized_value))
    except re.error:
        return False  # malformed regex — skip silently

# Rules loaded ORDER BY priority DESC. First match wins. No further rules evaluated.
# For artist tasks: match against log_artist.normalized_name
# For identity tasks: match against log_identity.normalized_signature
```

### 5.4 master_selection_service Scoring

```python
score = RELEASE_STATUS_SCORE + RELEASE_TYPE_SCORE + FORMAT_BONUS

RELEASE_STATUS_SCORE: promotion=100, official=0
RELEASE_TYPE_SCORE:   album=80, ep=70, single=60, compilation=40, live=30, other=20
FORMAT_BONUS:         flac=10, aac/ogg=6, mp3=3, other=1

Tiebreak 1: higher bitrate
Tiebreak 2: longer duration_ms
```

`recalculate()` skips any `song_masters` row where `selection_method='manual'`.

### 5.5 Normalization Pipeline

Implemented in `backend/services/normalization.py`. Steps execute in order:

1. Smart quote → ASCII quotes
2. Unicode NFKD decomposition (decomposes full-width chars, ligatures)
3. Strip combining marks (accent removal)
4. Lowercase
5. Strip feat. suffixes (all variants: feat., ft., featuring, (feat.), [ft.], etc.)
6. Strip leading articles ("the ", "a ", "an ")
7. Remove remaster/year/truncation markers (parenthesized years, "remaster", trailing `…`)
8. `&` → `and`, `+` → `plus`
9. Strip special characters (`[^\w\s]` → removed)
10. Collapse whitespace

`normalized_signature = hashlib.md5(f"{normalize_artist(artist)}||{normalize_title(title)}".encode('utf-8')).hexdigest()`

The `||` separator prevents collision between `("the", "beatles song")` and `("the beatles", "song")`.

Known limitations:
- "Earth, Wind & Fire" — internal comma looks like a split signal but isn't; deferred to manual rule
- Version keyword false positives — "Olive" → LIVE, "Democracy" → DEMO without word-boundary guards
- Bare em dash (U+2014) not caught by smart-quote normalization

---

## 6. Library Pipeline

### 6.1 Scanner (`library_scan_service.py`)

Uses `mutagen` for tag extraction. Handles: FLAC, MP3, AAC (.m4a), OGG, WAV. Three tag tiers:

- **Tier 1 (well-tagged):** All MusicBrainz tag fields present. `recording_mbid`, `artist_mbid`, `album_artist_mbid`, `release_mbid` extracted. Enables MBID-exact matching with zero API calls.
- **Tier 2 (partial):** Some MBIDs missing. Falls through to normalization matching.
- **Tier 3 (poor/untagged):** Only filename available. Falls through to vector or NEEDS_REVIEW.

Tag extraction: `mutagen` returns lists — always take `[0]`, handle `KeyError`/`IndexError`. Track number: parse slash notation (`"6/8"` → `6`).

Change detection on rescan: `file_path` + `file_hash` (SHA-256). If hash unchanged → skip. If hash changed → re-extract. If path gone → configurable (mark for review or remove). New paths → insert.

**Featured artist detection:**
```sql
artist_mbid IS NOT NULL
AND album_artist_mbid IS NOT NULL
AND artist_mbid != album_artist_mbid
```

### 6.2 Enrichment (`library_enrichment_service.py`)

Scope: `library_files` rows with `enrichment_status='pending'`.
Processing order: batch by `release_mbid` (album batching for API efficiency) then by `recording_mbid`.
Output: populates `library_files.recording_id`, updates `enrichment_status`.
API calls: `lookup_release()` and `lookup_recording()` via `mb_client`.

Distinct from `mb_enrichment_tasks.py` which fills metadata on canonical entities that already exist (artists/works/recordings with `needs_enhancement=TRUE`). Run sequence: library enrichment first → MB enrichment second.

### 6.3 MusicBrainz API (`mb_client.py`)

Rate-limited to 1.1s per request via `time.sleep(1.1)` (module-level, between calls). Single-worker constraint ensures no concurrent MB API calls. `httpx` as HTTP client. All responses cached in `mb_cache` table to prevent redundant API calls.

---

## 7. Frontend Structure

**Stack:** React 19, TypeScript 5.6, Vite 6, TanStack Query v5, React Router v7, Tailwind CSS v4, Zod v3, Lucide React, TanStack Virtual.

**Dev server:** port 5173. Backend: `http://127.0.0.1:8000`. WebSocket: `ws://127.0.0.1:8000/ws?token=...` (token as query param — browser WebSocket API does not support custom headers).

### 7.1 Route Tree

```typescript
createBrowserRouter([{
  path: '/',
  element: <App />,   // Sidebar + Outlet
  children: [
    { index: true, element: <Navigate to="/stations" replace /> },
    { path: 'stations', children: [
        { index: true, element: <StationList /> },
        { path: ':station_id', element: <StationDashboard /> },
        { path: ':station_id/playlists', element: <PlaylistViewer /> },
    ]},
    { path: 'library', children: [
        { index: true, element: <LibraryStatus /> },
        { path: 'artists', element: <ArtistBrowser /> },
        { path: 'artists/:artist_id', element: <ArtistDetail /> },
        { path: 'artists/:artist_id/works/:work_id', element: <AssociatedWorks /> },
    ]},
    { path: 'matcher', children: [
        { index: true, element: <MatcherBrowser /> },
        { path: 'scanner', element: <ScannerActions /> },
    ]},
    { path: 'settings', children: [
        { index: true, element: <Settings /> },
        { path: 'paths', element: <PathConfiguration /> },
    ]},
  ]
}])
```

### 7.2 Directory Structure

```
frontend/src/
├── main.tsx
├── App.tsx                          # Shell: Sidebar + <Outlet>
├── api/                             # TanStack Query hooks + typed fetchers
│   ├── client.ts                    # Base fetcher, X-Airwave-Token header, ApiError hierarchy
│   ├── stations.ts
│   ├── ingestion.ts
│   ├── library.ts
│   ├── artists.ts
│   ├── works.ts
│   ├── matcher.ts
│   ├── playlists.ts
│   ├── tasks.ts                     # useActiveTasks()
│   └── settings.ts
├── components/
│   ├── layout/
│   │   ├── Sidebar.tsx
│   │   └── ProgressBar.tsx          # WebSocket consumer, multi-task state machine
│   ├── ui/                          # Shared primitives (Badge, Modal, Toast, Table, etc.)
│   └── domain/
│       ├── matcher/
│       │   ├── ArtistPanel.tsx      # Step 1: artist resolution
│       │   ├── TitlePanel.tsx       # Step 2: title/file resolution (disabled until artist confirmed)
│       │   └── SearchSlideOver.tsx  # Right-side search (artist mode + file mode)
│       ├── works/
│       │   ├── WorkFilesTable.tsx
│       │   └── FormatOverridePanel.tsx
│       ├── playlists/
│       │   ├── PlaylistEventTable.tsx  # Paginated events, status badges, filter checkboxes
│       │   └── DatePicker.tsx          # Calendar with broadcast_days highlights
│       ├── stations/
│       │   └── StationForm.tsx         # Add/Edit form, format_name rename warning
│       └── library/
│           └── FeaturedReleasesSection.tsx
├── pages/
│   ├── stations/    — StationList, StationDashboard, PlaylistViewer
│   ├── library/     — LibraryStatus, ArtistBrowser, ArtistDetail, AssociatedWorks
│   ├── matcher/     — MatcherBrowser, ScannerActions
│   └── settings/    — Settings, PathConfiguration
├── hooks/
│   └── useWebSocket.ts              # Exponential backoff (1s→30s ±20%), reconnect → useActiveTasks()
├── store/
│   └── progressStore.ts             # Zustand; keyed by task_id; "+N more" for concurrent tasks
└── lib/
    ├── schemas/
    │   ├── stations.ts
    │   ├── playlists.ts
    │   ├── library.ts
    │   ├── artists.ts
    │   ├── works.ts
    │   ├── matches.ts
    │   ├── matcher.ts
    │   ├── tasks.ts
    │   ├── settings.ts
    │   └── index.ts                 # Re-exports all schemas
    └── utils.ts                     # formatDuration(), formatDate(), formatConfidence()
```

### 7.3 Key Behavioral Constraints

**Implementation order per domain:** `lib/schemas/{domain}.ts` → `api/{domain}.ts` → `components/domain/` → `pages/`. Zod schema is the contract; hook return types are inferred via `z.infer<typeof Schema>`.

**`api/client.ts` error contract:** All errors extend `ApiError({ status, message })`. Subclasses: `AuthError` (401), `ConflictError` (409 — used by ingestion duplicate detection), `ValidationError` (422 with FastAPI detail array), `ServerError` (5xx). Zod validation on responses in dev builds only. TanStack Query surfaces errors via `isError + error.message` per component — no global error boundary redirect.

**`ProgressBar.tsx` state machine:** `IDLE → RUNNING → COMPLETED (2s) → IDLE` / `FAILED (user-dismissed)`. Multiple concurrent tasks: show most-recently-started task with "+N more" indicator. On WebSocket reconnect, immediately call `useActiveTasks()` before next 500ms poll.

**`ArtistBrowser.tsx`:** TanStack Virtual (`useVirtualizer`) + `useInfiniteQuery`. Server-side pagination `GET /api/v1/library/artists?limit=50&offset=N`. Fixed row height 56px. Next page triggered when scroll within 200px of bottom. Search (debounced 300ms) resets to page 0.

**`AssociatedWorks.tsx` master toggle:** Optimistic update via TanStack Query `useMutation`. `onMutate` updates local cache immediately. `onError` rolls back. Crown icons: solid = manual master, outline = auto master, ghost on hover = non-master.

**`SearchSlideOver.tsx` file mode:** Default filter ON (`artist_mbid=confirmedMbid`). Toggle "Restrict to confirmed artist." Selecting a result calls `onSelect(libraryFile)` prop — no write. Commit happens in parent `TitlePanel` via Confirm button.

---

## 8. Build Sequence (42 Sessions)

### Phase 0 — Foundation

| Session | Scope | Gate |
|---------|-------|------|
| 0-1 | Project scaffold: `pyproject.toml`, `package.json`, `Procfile`, `.env.example` | Files exist, `uv sync` succeeds |
| 0-2 | All 9 migration SQL files — pure SQL, no Python | Schema review against this spec |
| 0-3 | `db/migrations.py` + `schema_migrations` bootstrap + integration test | `uv run pytest tests/integration/` passes on all 9 migrations |
| 0-4 | `domain/enums.py`, `config.py`, `logging_config.py`, `db/pool.py` | `uv run mypy --strict backend/` passes |
| 0-5 | All 19 repository ABCs + matching `tests/fakes/` (written together) | Each fake implements its ABC; no SQL |
| 0-6 | All Zod schema stubs (`lib/schemas/*.ts` — empty `z.object({})`) | `tsc --noEmit` passes |

**Phase 0 gate:** Migrations apply cleanly on a fresh DB AND TypeScript compiles.

### Phase 1 — Data Pipeline

| Session | Scope | Gate |
|---------|-------|------|
| 1-1 | `normalization.py` — all functions + 100+ test cases | All tests pass, `mypy` clean |
| 1-2 | `db/repositories/log_artists.py` + `db/repositories/log_identities.py` | Integration tests against real test DB |
| 1-3 | `ingestion_service.py` + `db/repositories/playlists.py` + `log_events.py` + `broadcast_days.py` | Integration test ingesting KAZR sample; verify row counts |
| 1-4 | `ingestion_tasks.py` + `POST /api/v1/ingestion/playlists` router | `curl` POST KAZR CSV; verify DB rows |
| 1-5 | `embedding_service.py` singleton + `embedding_tasks.py` (playlist-scoped) | Vectors populated; RAM ≤2GB verified on real hardware |
| 1-6 | `db/repositories/artists.py` + `artist_matching_service.py` (Tiers 1–3) + `artist_matching_tasks.py` | Unit tests with fake repos, all threshold cases |
| 1-7 | `mb_client.py` + `db/repositories/mb_cache.py` + artist Tier 4 (MB API) | Integration test against real MB API; 1.1s throttle verified |
| 1-8 | `db/repositories/recordings.py` + `works.py` + `identity_matching_service.py` + `identity_matching_tasks.py` + `master_selection_service.py` | End-to-end: KAZR CSV → full pipeline → `match_status` set, `song_masters` populated |

**Phase 1 gate:** Full pipeline runs on KAZR sample CSV. Verify DB state in psql before writing any frontend.

### Phase 2 — Library Pipeline

| Session | Scope | Gate |
|---------|-------|------|
| 2-1 | `library_scan_service.py` + `db/repositories/library_files.py` + `library_quarantine.py` + `library_tasks.py` + `POST /api/v1/library/scan` | Scan real music directory; verify all columns populated |
| 2-2 | `library_enrichment_service.py` + `library_enrichment_tasks.py` + `POST /api/v1/library/enrich` | `recording_id` populated on files; `needs_enhancement=TRUE` rows created |
| 2-3 | `mb_enrichment_tasks.py` | `artists/works/recordings` fully populated; `needs_enhancement=FALSE` |

### Phase 3 — API Layer

| Session | Scope | Gate |
|---------|-------|------|
| 3-1 | `routers/stations.py` — CRUD + aggregate stats query | API returns real data |
| 3-2 | `routers/playlists.py` — list, detail, events (paginated) | |
| 3-3 | `routers/library.py` — status, artists list, artist detail, works detail | |
| 3-4 | `routers/matching.py` — queue, resolve artist, resolve identity | |
| 3-5 | `routers/settings.py` — get/put user_settings | |
| 3-6 | `routers/tasks.py` — `GET /api/v1/tasks/active` | |
| 3-7 | `websocket.py` — broadcast loop, stale cleanup, `?token=` auth | |
| 3-8 | `m3u_generator_service.py` + `POST .../export-m3u` endpoint | Navidrome path mapping verified |

Each router session also fleshes out the corresponding Zod schema stubs from Phase 0.

**Phase 3 gate:** All API endpoints return real data from the running backend before Phase 4 begins.

### Phase 4 — Frontend

| Session | Scope | Gate |
|---------|-------|------|
| 4-1 | `useWebSocket.ts` + `progressStore.ts` + `ProgressBar.tsx` | Progress bar updates during a scan |
| 4-2 | `api/client.ts` + `api/stations.ts` + `lib/schemas/stations.ts` + `StationForm.tsx` | |
| 4-3 | `StationList.tsx` | Renders real station data |
| 4-4 | `StationDashboard.tsx` | |
| 4-5 | `api/playlists.ts` + `lib/schemas/playlists.ts` + `PlaylistEventTable.tsx` + `DatePicker.tsx` | |
| 4-6 | `PlaylistViewer.tsx` | |
| 4-7 | `api/matcher.ts` + `lib/schemas/matcher.ts` + `ArtistPanel.tsx` + `TitlePanel.tsx` | |
| 4-8 | `SearchSlideOver.tsx` (artist mode + file mode) | |
| 4-9 | `MatcherBrowser.tsx` | End-to-end artist-first resolution workflow |
| 4-10 | `ScannerActions.tsx` | |
| 4-11 | `api/library.ts` + `lib/schemas/library.ts` + `LibraryStatus.tsx` | |
| 4-12 | `ArtistBrowser.tsx` (virtual scrolling, infinite query) | |
| 4-13 | `api/artists.ts` + `lib/schemas/artists.ts` + `ArtistDetail.tsx` Section A (primary works) | |
| 4-14 | `ArtistDetail.tsx` Section B + `FeaturedReleasesSection.tsx` | |
| 4-15 | `api/works.ts` + `lib/schemas/works.ts` + `WorkFilesTable.tsx` + `FormatOverridePanel.tsx` | |
| 4-16 | `AssociatedWorks.tsx` | Optimistic master toggle verified |
| 4-17 | `Settings.tsx` + `PathConfiguration.tsx` + `api/settings.ts` | |

**Phase 4 gate:** Each page renders with real data before starting the next page.

---

## 9. Two Rules That Prevent Drift

**Rule 1 — Schema reference in every backend session.** Every LLM session that writes backend code receives `docs/schema-reference.md` as the first attached document. Not a summary — the full document. Prevents column name drift, wrong types, and invented tables.

**Rule 2 — Gate before proceeding.** Do not start the next session until the gate condition passes. The three most important gates:
1. Phase 0: migrations apply cleanly + TypeScript compiles
2. Phase 1: full pipeline runs on KAZR sample CSV, verified in psql
3. Phase 3→4 boundary: all API endpoints return real data before any frontend page is written

The most common failure mode in the original RetroStation project was frontend sessions written against assumed API shapes that hadn't been built yet, then backend sessions that implemented slightly different shapes, then rework to reconcile. Phase 3 must be complete before Phase 4 begins.

---

## 10. Non-Functional Constraints

- **RAM:** Total application footprint target ≤2GB. BGE-M3 model (~1.1GB) loads only in worker process.
- **Embedding throughput:** ≥20 embeddings/second on Ryzen 7 3700X (CPU-only). ~75s for 1,500 identities.
- **MB API rate limit:** 1.1s per request, enforced in `mb_client.py`. Single worker prevents concurrent calls.
- **Type safety:** `mypy --strict` with zero errors. `ruff check` with zero warnings. No `Any` except documented third-party stub gaps. No `# type: ignore` except documented.
- **Test database:** `retrostation_test`. Migrations applied fresh per test session by the same runner as production.
- **No Docker, no WSL.** All processes run natively on Windows 11 Home.
