# RetroStation Phase 0 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a verified project scaffold — working PostgreSQL migrations, typed domain models, repository ABCs with in-memory fakes, and TypeScript Zod schema stubs — so every subsequent phase has a stable foundation to build on.

**Architecture:** Python backend managed by `uv` with a custom migration runner that applies 9 numbered SQL files in order. All domain entities are Python dataclasses. Business logic depends only on repository ABCs; PostgreSQL implementations and in-memory fakes both implement the same interface. Frontend is a React 19 + Vite scaffold with stubbed Zod schemas that will be fleshed out in Phase 3.

**Tech Stack:** Python 3.13+, uv, FastAPI, psycopg[binary]>=3.1, psycopg-pool>=3.1, structlog, pydantic-settings, pytest; React 19, TypeScript 5.6, Vite 6, Zod v3, TanStack Query v5, React Router v7, Tailwind CSS v4.

**Spec reference:** `docs/superpowers/specs/2026-03-31-retrostation-design.md` — consult Section 3.5 for all column names and types before writing any SQL or Python.

---

## File Structure

```
D:/PythonStuff/RetroStation/
├── pyproject.toml
├── .python-version
├── .env.example
├── Procfile
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── logging_config.py
│   ├── dependencies.py
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── enums.py
│   │   └── models.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── pool.py
│   │   ├── migrations.py
│   │   └── migrations/
│   │       ├── 0001_observation_layer.sql
│   │       ├── 0002_canonical_layer.sql
│   │       ├── 0003_matching_layer.sql
│   │       ├── 0004_library_layer.sql
│   │       ├── 0005_vector_indexes.sql
│   │       ├── 0006_settings_and_ops.sql
│   │       ├── 0007_stations.sql
│   │       ├── 0008_song_masters.sql
│   │       └── 0009_mb_cache.sql
│   └── repositories/
│       ├── __init__.py
│       ├── stations.py          ← ABC
│       ├── playlists.py
│       ├── broadcast_days.py
│       ├── log_artists.py
│       ├── log_identities.py
│       ├── log_events.py
│       ├── artists.py
│       ├── works.py
│       ├── recordings.py
│       ├── library_files.py
│       ├── library_quarantine.py
│       ├── matches.py
│       ├── song_masters.py
│       ├── format_overrides.py
│       ├── global_mapping_rules.py
│       ├── mb_cache.py
│       ├── progress_tracking.py
│       └── settings.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── fakes/
│   │   ├── __init__.py
│   │   ├── stations.py          ← in-memory fake
│   │   ├── playlists.py
│   │   ├── broadcast_days.py
│   │   ├── log_artists.py
│   │   ├── log_identities.py
│   │   ├── log_events.py
│   │   ├── artists.py
│   │   ├── works.py
│   │   ├── recordings.py
│   │   ├── library_files.py
│   │   ├── library_quarantine.py
│   │   ├── matches.py
│   │   ├── song_masters.py
│   │   ├── format_overrides.py
│   │   ├── global_mapping_rules.py
│   │   ├── mb_cache.py
│   │   ├── progress_tracking.py
│   │   └── settings.py
│   └── integration/
│       ├── __init__.py
│       └── test_migrations.py
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    └── src/
        ├── main.tsx
        └── lib/
            └── schemas/
                ├── stations.ts
                ├── playlists.ts
                ├── library.ts
                ├── artists.ts
                ├── works.ts
                ├── matches.ts
                ├── matcher.ts
                ├── tasks.ts
                ├── settings.ts
                └── index.ts
```

---

## Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.env.example`
- Create: `Procfile`
- Create: `backend/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "retrostation"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.134",
    "uvicorn[standard]",
    "psycopg[binary]>=3.1",
    "psycopg-pool>=3.1",
    "huey>=2.6",
    "mutagen>=1.47",
    "sentence-transformers>=3.0",
    "rapidfuzz>=3.0",
    "httpx>=0.27",
    "structlog",
    "pydantic-settings",
    "pgvector",
    "chardet",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio",
    "mypy>=1.11",
    "ruff>=0.6",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.mypy]
strict = true
python_version = "3.13"
ignore_missing_imports = false

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```

- [ ] **Step 2: Create `.python-version`**

```
3.13
```

- [ ] **Step 3: Create `.env.example`**

```
DATABASE_URL=postgresql://retrostation:retrostation-dev@localhost:5432/retrostation
AIRWAVE_TOKEN=change-me-before-use
LOG_LEVEL=INFO
```

- [ ] **Step 4: Create `Procfile`**

```
api:    uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
worker: uv run python -m huey.bin.huey_consumer backend.tasks.huey_app.huey -w 1 -n
web:    cd frontend && npm run dev
```

- [ ] **Step 5: Create `backend/__init__.py`** (empty file)

```python
```

- [ ] **Step 6: Install dependencies**

```
uv sync
```

Expected: resolves all packages, creates `uv.lock`, no errors.

- [ ] **Step 7: Commit**

```bash
git init
git add pyproject.toml .python-version .env.example Procfile backend/__init__.py
git commit -m "chore: project scaffold — pyproject.toml, Procfile, env template"
```

---

## Task 2: Migration SQL Files — Observation + Canonical + Matching + Library

**Files:**
- Create: `backend/db/__init__.py`
- Create: `backend/db/migrations/0001_observation_layer.sql`
- Create: `backend/db/migrations/0002_canonical_layer.sql`
- Create: `backend/db/migrations/0003_matching_layer.sql`
- Create: `backend/db/migrations/0004_library_layer.sql`

- [ ] **Step 1: Create `backend/db/__init__.py`** (empty)

```python
```

- [ ] **Step 2: Create `backend/db/migrations/0001_observation_layer.sql`**

```sql
-- Observation layer: raw radio log data. Never edited after ingestion.

CREATE TABLE playlists (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    content_hash TEXT        NOT NULL UNIQUE,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    -- station_id UUID added in 0007_stations.sql
);

CREATE TABLE log_artists (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    original_name     TEXT        NOT NULL,
    normalized_name   TEXT        NOT NULL UNIQUE,
    match_status      TEXT        NOT NULL DEFAULT 'PENDING',
    artist_candidates JSONB,
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE TABLE log_identities (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    artist_id            UUID        NOT NULL REFERENCES log_artists(id),
    original_title       TEXT        NOT NULL,
    normalized_title     TEXT        NOT NULL,
    normalized_signature TEXT        NOT NULL UNIQUE,
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

- [ ] **Step 3: Create `backend/db/migrations/0002_canonical_layer.sql`**

```sql
-- Canonical layer: MusicBrainz entities. PKs are MBIDs (TEXT).

CREATE TABLE artists (
    id                TEXT        PRIMARY KEY,
    name              TEXT        NOT NULL,
    sort_name         TEXT        NOT NULL,
    disambiguation    TEXT,
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
);

CREATE TABLE works (
    id                TEXT        PRIMARY KEY,
    title             TEXT        NOT NULL,
    artist_id         TEXT        NOT NULL REFERENCES artists(id),
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE INDEX idx_works_artist ON works(artist_id);

CREATE TABLE recordings (
    id                TEXT        PRIMARY KEY,
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

- [ ] **Step 4: Create `backend/db/migrations/0003_matching_layer.sql`**

```sql
-- Matching layer: links log entries to canonical entities.
-- NOTE: matches.library_file_id has no FK here — library_files doesn't exist
-- until 0004. The FK constraint is added via ALTER TABLE in 0004.

CREATE TABLE matches (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id      UUID        REFERENCES log_identities(id),
    artist_id        UUID        REFERENCES log_artists(id),
    library_file_id  UUID,
    target_id        TEXT,
    target_type      TEXT,
    confidence_score REAL        NOT NULL DEFAULT 0.0,
    match_tier       TEXT        NOT NULL DEFAULT 'UNKNOWN',
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
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_pattern TEXT        NOT NULL,
    target_type    TEXT        NOT NULL,
    target_id      TEXT        NOT NULL,
    priority       INTEGER     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_rules_priority ON global_mapping_rules(priority DESC);
```

- [ ] **Step 5: Create `backend/db/migrations/0004_library_layer.sql`**

```sql
-- Library layer: local audio files.

CREATE TABLE library_files (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path            TEXT        NOT NULL UNIQUE,
    file_hash            TEXT        NOT NULL,
    indexed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id             TEXT,
    recording_id         TEXT        REFERENCES recordings(id),
    recording_mbid       TEXT,
    artist_mbid          TEXT,
    album_artist_mbid    TEXT,
    release_mbid         TEXT,
    release_title        TEXT,
    release_type         TEXT,
    release_type_secondary TEXT,
    release_status       TEXT,
    track_title          TEXT,
    track_number         SMALLINT,
    disc_number          SMALLINT,
    duration_ms          INTEGER,
    format               TEXT        NOT NULL DEFAULT 'unknown',
    bitrate              INTEGER,
    enrichment_status    TEXT        NOT NULL DEFAULT 'pending',
    raw_metadata         JSONB
);

CREATE INDEX idx_library_files_enrichment_album
    ON library_files(enrichment_status, release_mbid)
    WHERE enrichment_status = 'pending' AND release_mbid IS NOT NULL;

CREATE INDEX idx_library_files_enrichment_recording
    ON library_files(enrichment_status, recording_mbid)
    WHERE enrichment_status = 'pending'
      AND recording_mbid IS NOT NULL
      AND release_mbid IS NULL;

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

-- Deferred FK: library_files now exists
ALTER TABLE matches
    ADD CONSTRAINT fk_matches_library_file
    FOREIGN KEY (library_file_id) REFERENCES library_files(id);
```

- [ ] **Step 6: Commit**

```bash
git add backend/db/
git commit -m "feat: migrations 0001-0004 — observation, canonical, matching, library layers"
```

---

## Task 3: Migration SQL Files — Vectors + Settings + Stations + Masters + Cache

**Files:**
- Create: `backend/db/migrations/0005_vector_indexes.sql`
- Create: `backend/db/migrations/0006_settings_and_ops.sql`
- Create: `backend/db/migrations/0007_stations.sql`
- Create: `backend/db/migrations/0008_song_masters.sql`
- Create: `backend/db/migrations/0009_mb_cache.sql`

- [ ] **Step 1: Create `backend/db/migrations/0005_vector_indexes.sql`**

```sql
-- pgvector extension + embedding columns on 4 tables + HNSW indexes.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE log_artists    ADD COLUMN embedding vector(1024);
ALTER TABLE log_identities ADD COLUMN embedding vector(1024);
ALTER TABLE works          ADD COLUMN embedding vector(1024);
ALTER TABLE recordings     ADD COLUMN embedding vector(1024);

CREATE INDEX idx_log_artists_embedding
    ON log_artists USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_log_identities_embedding
    ON log_identities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_works_embedding
    ON works USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_recordings_embedding
    ON recordings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
```

- [ ] **Step 2: Create `backend/db/migrations/0006_settings_and_ops.sql`**

```sql
-- Settings key-value store, structured log table, background task tracking.

CREATE TABLE user_settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE system_logs (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id   TEXT,
    category   TEXT        NOT NULL,
    level      TEXT        NOT NULL,
    message    TEXT        NOT NULL,
    details    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_system_logs_created ON system_logs(created_at DESC);
CREATE INDEX idx_system_logs_level   ON system_logs(level);

CREATE TABLE progress_tracking (
    task_id       TEXT        PRIMARY KEY,
    task_type     TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'running',
    progress_data JSONB       NOT NULL DEFAULT '{}',
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX idx_progress_status_time ON progress_tracking(status, updated_at);
CREATE INDEX idx_progress_type_status ON progress_tracking(task_type, status);
CREATE INDEX idx_progress_stale
    ON progress_tracking(updated_at)
    WHERE status = 'running';
```

- [ ] **Step 3: Create `backend/db/migrations/0007_stations.sql`**

```sql
-- Radio stations + broadcast day calendar + deferred FKs from 0001.

CREATE TABLE stations (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_letters TEXT        NOT NULL UNIQUE,
    name         TEXT,
    city         TEXT,
    format_name  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE broadcast_days (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id     UUID NOT NULL REFERENCES stations(id),
    broadcast_date DATE NOT NULL,
    UNIQUE (station_id, broadcast_date)
);

CREATE INDEX idx_broadcast_days_station ON broadcast_days(station_id);

-- Deferred FKs (stations and broadcast_days now exist)
ALTER TABLE playlists  ADD COLUMN station_id      UUID REFERENCES stations(id) ON DELETE SET NULL;
ALTER TABLE log_events ADD COLUMN broadcast_day_id UUID REFERENCES broadcast_days(id);

CREATE INDEX idx_playlists_station ON playlists(station_id);
```

- [ ] **Step 4: Create `backend/db/migrations/0008_song_masters.sql`**

```sql
-- Master file selection per work + per-station-format overrides.

CREATE TABLE song_masters (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT        NOT NULL REFERENCES works(id),
    preferred_file_id UUID        NOT NULL REFERENCES library_files(id),
    selection_method  TEXT        NOT NULL DEFAULT 'auto',
    score             INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_id)
);

CREATE TABLE format_overrides (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT        NOT NULL REFERENCES works(id),
    format_name       TEXT        NOT NULL,
    preferred_file_id UUID        NOT NULL REFERENCES library_files(id),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_id, format_name)
);

CREATE INDEX idx_format_overrides_work   ON format_overrides(work_id);
CREATE INDEX idx_format_overrides_format ON format_overrides(format_name);
```

- [ ] **Step 5: Create `backend/db/migrations/0009_mb_cache.sql`**

```sql
-- MusicBrainz API response cache. Prevents redundant API calls.

CREATE TABLE mb_cache (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key     TEXT        NOT NULL UNIQUE,
    entity_type   TEXT        NOT NULL,
    entity_mbid   TEXT        NOT NULL,
    response_data JSONB       NOT NULL,
    cached_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_mb_cache_entity ON mb_cache(entity_type, entity_mbid);
CREATE INDEX idx_mb_cache_expiry ON mb_cache(expires_at);
```

- [ ] **Step 6: Commit**

```bash
git add backend/db/migrations/
git commit -m "feat: migrations 0005-0009 — vectors, settings, stations, masters, mb_cache"
```

---

## Task 4: Migration Runner + Integration Tests

**Files:**
- Create: `backend/db/migrations.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_migrations.py`

Prerequisite: PostgreSQL 16 service running. `retrostation_test` database must exist:
```sql
CREATE DATABASE retrostation_test;
CREATE USER retrostation WITH PASSWORD 'retrostation-dev';
GRANT ALL PRIVILEGES ON DATABASE retrostation_test TO retrostation;
```
pgvector extension must be installed in PostgreSQL (copy `vector.dll`, `vector.control`, `vector--*.sql` to PG extension dirs).

- [ ] **Step 1: Create `backend/db/migrations.py`**

```python
import logging
from pathlib import Path
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations(conn: psycopg.Connection[Any]) -> None:
    """Apply all pending numbered migrations in ascending order."""
    _ensure_migrations_table(conn)
    applied = _get_applied_versions(conn)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.warning("No migration files found in %s", MIGRATIONS_DIR)
        return

    for migration_file in migration_files:
        version = migration_file.stem  # e.g. "0001_observation_layer"
        if version in applied:
            logger.debug("Migration %s already applied, skipping", version)
            continue

        logger.info("Applying migration %s", version)
        sql = migration_file.read_text(encoding="utf-8")

        try:
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
        except Exception as exc:
            logger.error("Migration %s failed: %s", version, exc)
            raise RuntimeError(f"Migration {version} failed: {exc}") from exc

    logger.info("All migrations applied successfully")


def _ensure_migrations_table(conn: psycopg.Connection[Any]) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT        PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.commit()


def _get_applied_versions(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return {row[0] for row in rows}
```

- [ ] **Step 2: Create `tests/__init__.py`** (empty)

```python
```

- [ ] **Step 3: Create `tests/conftest.py`**

```python
import os
import pytest
import psycopg
from psycopg.rows import dict_row

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation_test",
)


@pytest.fixture(scope="session")
def db_url() -> str:
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def clean_db(db_url: str) -> None:
    """Drop and recreate public schema for a clean slate."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="session")
def migrated_db(clean_db: None, db_url: str) -> str:
    from backend.db.migrations import run_migrations
    with psycopg.connect(db_url) as conn:
        run_migrations(conn)
        conn.commit()
    return db_url
```

- [ ] **Step 4: Create `tests/integration/__init__.py`** (empty)

```python
```

- [ ] **Step 5: Create `tests/integration/test_migrations.py`**

```python
import psycopg
import pytest


def test_all_nine_migrations_applied(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    versions = [r[0] for r in rows]
    assert len(versions) == 9
    assert versions[0].startswith("0001")
    assert versions[8].startswith("0009")


def test_all_expected_tables_exist(migrated_db: str) -> None:
    expected = {
        "playlists", "log_artists", "log_identities", "log_events",
        "artists", "works", "recordings",
        "matches", "global_mapping_rules",
        "library_files", "library_quarantine",
        "user_settings", "system_logs", "progress_tracking",
        "stations", "broadcast_days",
        "song_masters", "format_overrides",
        "mb_cache",
    }
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"Missing tables: {missing}"


def test_pgvector_extension_installed(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert row is not None, "pgvector extension not installed"


def test_embedding_columns_on_four_tables(migrated_db: str) -> None:
    tables = ["log_artists", "log_identities", "works", "recordings"]
    with psycopg.connect(migrated_db) as conn:
        for table in tables:
            row = conn.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'embedding'
            """, (table,)).fetchone()
            assert row is not None, f"{table}.embedding column missing"


def test_deferred_fk_columns_exist(migrated_db: str) -> None:
    checks = [
        ("playlists",  "station_id"),
        ("log_events", "broadcast_day_id"),
    ]
    with psycopg.connect(migrated_db) as conn:
        for table, column in checks:
            row = conn.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
            """, (table, column)).fetchone()
            assert row is not None, f"{table}.{column} missing"


def test_matches_library_file_fk_constraint_exists(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        row = conn.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'matches'
              AND constraint_name = 'fk_matches_library_file'
        """).fetchone()
    assert row is not None, "matches.fk_matches_library_file constraint missing"


def test_migrations_idempotent(migrated_db: str) -> None:
    """Running migrations a second time must be a no-op."""
    from backend.db.migrations import run_migrations
    with psycopg.connect(migrated_db) as conn:
        run_migrations(conn)  # second run — must not raise


def test_xor_constraint_on_matches(migrated_db: str) -> None:
    """The XOR constraint on matches must reject rows with both FKs set."""
    import uuid
    with psycopg.connect(migrated_db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("""
                    INSERT INTO matches (identity_id, artist_id, confidence_score, match_tier)
                    VALUES (%s, %s, 0.9, 'MANUAL')
                """, (uuid.uuid4(), uuid.uuid4()))
```

- [ ] **Step 6: Run the integration tests**

```
uv run pytest tests/integration/test_migrations.py -v
```

Expected output:
```
tests/integration/test_migrations.py::test_all_nine_migrations_applied PASSED
tests/integration/test_migrations.py::test_all_expected_tables_exist PASSED
tests/integration/test_migrations.py::test_pgvector_extension_installed PASSED
tests/integration/test_migrations.py::test_embedding_columns_on_four_tables PASSED
tests/integration/test_migrations.py::test_deferred_fk_columns_exist PASSED
tests/integration/test_migrations.py::test_matches_library_file_fk_constraint_exists PASSED
tests/integration/test_migrations.py::test_migrations_idempotent PASSED
tests/integration/test_migrations.py::test_xor_constraint_on_matches PASSED

8 passed in X.XXs
```

- [ ] **Step 7: Commit**

```bash
git add backend/db/migrations.py tests/
git commit -m "feat: migration runner + integration tests — all 9 migrations verified"
```

---

## Task 5: Domain Foundation

**Files:**
- Create: `backend/domain/__init__.py`
- Create: `backend/domain/enums.py`
- Create: `backend/domain/models.py`
- Create: `backend/config.py`
- Create: `backend/logging_config.py`
- Create: `backend/db/pool.py`
- Create: `backend/dependencies.py`
- Create: `backend/main.py`

- [ ] **Step 1: Create `backend/domain/__init__.py`** (empty)

```python
```

- [ ] **Step 2: Create `backend/domain/enums.py`**

```python
from enum import Enum


class MatchStatus(str, Enum):
    PENDING       = "PENDING"
    AUTO_MATCHED  = "AUTO_MATCHED"
    NEEDS_REVIEW  = "NEEDS_REVIEW"
    MAN_MATCHED   = "MAN_MATCHED"
    AUTO_REJECTED = "AUTO_REJECTED"
    MAN_REJECTED  = "MAN_REJECTED"


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
    PROMOTION      = "promotion"
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

- [ ] **Step 3: Create `backend/domain/models.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.domain.enums import (
    EnrichmentStatus,
    MatchStatus,
    MatchTier,
    ReleaseStatus,
    ReleaseType,
    SelectionMethod,
    TargetType,
    TaskStatus,
    TaskType,
    VersionType,
)


@dataclass
class Station:
    id: UUID
    call_letters: str
    name: str | None = None
    city: str | None = None
    format_name: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Playlist:
    id: UUID
    name: str
    content_hash: str
    ingested_at: datetime = field(default_factory=datetime.utcnow)
    station_id: UUID | None = None


@dataclass
class BroadcastDay:
    id: UUID
    station_id: UUID
    broadcast_date: date


@dataclass
class LogArtist:
    id: UUID
    original_name: str
    normalized_name: str
    match_status: MatchStatus = MatchStatus.PENDING
    artist_candidates: list[dict[str, Any]] | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    embedding: list[float] | None = None


@dataclass
class LogIdentity:
    id: UUID
    artist_id: UUID
    original_title: str
    normalized_title: str
    normalized_signature: str
    match_status: MatchStatus = MatchStatus.PENDING
    match_tier: MatchTier | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    embedding: list[float] | None = None


@dataclass
class LogEvent:
    id: UUID
    identity_id: UUID
    playlist_id: UUID
    played_at: datetime
    broadcast_day_id: UUID | None = None


@dataclass
class Artist:
    id: str  # MBID
    name: str
    sort_name: str
    disambiguation: str | None = None
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None


@dataclass
class Work:
    id: str  # MBID
    title: str
    artist_id: str  # MBID
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    embedding: list[float] | None = None


@dataclass
class Recording:
    id: str  # MBID
    title: str
    work_id: str | None = None
    duration_ms: int | None = None
    version_type: VersionType = VersionType.ORIGINAL
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    embedding: list[float] | None = None


@dataclass
class LibraryFile:
    id: UUID
    file_path: str
    file_hash: str
    format: str
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    indexed_at: datetime = field(default_factory=datetime.utcnow)
    trace_id: str | None = None
    recording_id: str | None = None
    recording_mbid: str | None = None
    artist_mbid: str | None = None
    album_artist_mbid: str | None = None
    release_mbid: str | None = None
    release_title: str | None = None
    release_type: ReleaseType | None = None
    release_type_secondary: str | None = None
    release_status: ReleaseStatus | None = None
    track_title: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    bitrate: int | None = None
    raw_metadata: dict[str, Any] | None = None


@dataclass
class LibraryQuarantine:
    id: UUID
    file_path: str
    error_message: str
    trace_id: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Match:
    id: UUID
    confidence_score: float
    match_tier: MatchTier
    identity_id: UUID | None = None
    artist_id: UUID | None = None
    library_file_id: UUID | None = None
    target_id: str | None = None
    target_type: TargetType | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SongMaster:
    id: UUID
    work_id: str
    preferred_file_id: UUID
    selection_method: SelectionMethod = SelectionMethod.AUTO
    score: int | None = None
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class FormatOverride:
    id: UUID
    work_id: str
    format_name: str
    preferred_file_id: UUID
    notes: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class GlobalMappingRule:
    id: UUID
    source_pattern: str
    target_type: TargetType
    target_id: str
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class MbCache:
    id: UUID
    cache_key: str
    entity_type: str
    entity_mbid: str
    response_data: dict[str, Any]
    cached_at: datetime
    expires_at: datetime


@dataclass
class ProgressTracking:
    task_id: str
    task_type: TaskType
    status: TaskStatus
    progress_data: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


@dataclass
class UserSetting:
    key: str
    value: str
    updated_at: datetime = field(default_factory=datetime.utcnow)
```

- [ ] **Step 4: Create `backend/config.py`**

```python
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation"
    airwave_token: str = "dev-token"
    log_level: str = "INFO"
    mb_auto_link_score: int = 95
    mb_score_gap: int = 10
    mb_needs_review_threshold: int = 50
    library_scan_paths: list[str] = []

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: Create `backend/logging_config.py`**

```python
import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO") -> None:
    # Must reconfigure before any other logging to prevent cp1252 Unicode crashes on Windows
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if log_level == "DEBUG"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
```

- [ ] **Step 6: Create `backend/db/pool.py`**

```python
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("Connection pool not initialized. Call init_pool() first.")
    return _pool


def init_pool(database_url: str) -> ConnectionPool:
    global _pool
    _pool = ConnectionPool(
        conninfo=database_url,
        min_size=2,
        max_size=10,
        open=False,  # opened explicitly in lifespan
        kwargs={"row_factory": dict_row},
    )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
```

- [ ] **Step 7: Create `backend/dependencies.py`**

```python
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Header, status
from psycopg import AsyncConnection

from backend.config import get_settings
from backend.db.pool import get_pool


async def get_current_token(
    x_airwave_token: Annotated[str | None, Header()] = None,
) -> str:
    settings = get_settings()
    if x_airwave_token != settings.airwave_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Airwave-Token header",
        )
    return x_airwave_token


async def get_db_connection() -> AsyncGenerator[AsyncConnection, None]:  # type: ignore[type-arg]
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
```

- [ ] **Step 8: Create `backend/main.py`**

```python
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import psycopg
from fastapi import FastAPI

from backend.config import get_settings
from backend.db.migrations import run_migrations
from backend.db.pool import close_pool, init_pool
from backend.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = init_pool(settings.database_url)
    pool.open()

    # Run migrations synchronously before accepting requests
    with psycopg.connect(settings.database_url) as conn:
        run_migrations(conn)
        conn.commit()

    yield

    close_pool()


app = FastAPI(title="RetroStation", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 9: Verify mypy passes**

```
uv run mypy --strict backend/domain/ backend/config.py backend/logging_config.py backend/db/pool.py backend/dependencies.py backend/main.py
```

Expected: `Success: no issues found in N source files`

- [ ] **Step 10: Commit**

```bash
git add backend/domain/ backend/config.py backend/logging_config.py backend/db/pool.py backend/dependencies.py backend/main.py
git commit -m "feat: domain foundation — enums, models, config, logging, pool, app skeleton"
```

---

## Task 6: Repository ABCs

**Files:**
- Create: `backend/repositories/__init__.py`
- Create: `backend/repositories/stations.py` through `backend/repositories/settings.py` (19 files)

- [ ] **Step 1: Create `backend/repositories/__init__.py`** (empty)

```python
```

- [ ] **Step 2: Create `backend/repositories/stations.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import Station


class StationRepository(ABC):
    @abstractmethod
    def create(self, station: Station) -> Station: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> Station | None: ...

    @abstractmethod
    def get_by_call_letters(self, call_letters: str) -> Station | None: ...

    @abstractmethod
    def list_all(self) -> list[Station]: ...

    @abstractmethod
    def update(self, station: Station) -> Station: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...
```

- [ ] **Step 3: Create `backend/repositories/playlists.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import Playlist


class PlaylistRepository(ABC):
    @abstractmethod
    def create(self, playlist: Playlist) -> Playlist: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> Playlist | None: ...

    @abstractmethod
    def get_by_content_hash(self, content_hash: str) -> Playlist | None: ...

    @abstractmethod
    def list_by_station(self, station_id: UUID) -> list[Playlist]: ...
```

- [ ] **Step 4: Create `backend/repositories/broadcast_days.py`**

```python
from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

from backend.domain.models import BroadcastDay


class BroadcastDayRepository(ABC):
    @abstractmethod
    def get_or_create(self, station_id: UUID, broadcast_date: date) -> BroadcastDay: ...

    @abstractmethod
    def get_dates_for_station(self, station_id: UUID) -> list[date]: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> BroadcastDay | None: ...
```

- [ ] **Step 5: Create `backend/repositories/log_artists.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogArtist


class LogArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: LogArtist) -> LogArtist:
        """Insert or ignore on normalized_name conflict. Always returns the stored row."""
        ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> LogArtist | None: ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> LogArtist | None: ...

    @abstractmethod
    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        """Artists linked to this playlist's events with match_status=PENDING."""
        ...

    @abstractmethod
    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        """Artists linked to this playlist's events with embedding IS NULL."""
        ...

    @abstractmethod
    def update_match_status(
        self,
        id: UUID,
        status: MatchStatus,
        tier: MatchTier | None = None,
    ) -> None: ...

    @abstractmethod
    def update_embedding(self, id: UUID, embedding: list[float]) -> None: ...
```

- [ ] **Step 6: Create `backend/repositories/log_identities.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogIdentity


class LogIdentityRepository(ABC):
    @abstractmethod
    def upsert(self, identity: LogIdentity) -> LogIdentity:
        """Insert or ignore on normalized_signature conflict. Always returns stored row."""
        ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> LogIdentity | None: ...

    @abstractmethod
    def get_by_signature(self, normalized_signature: str) -> LogIdentity | None: ...

    @abstractmethod
    def get_for_artist(self, artist_id: UUID) -> list[LogIdentity]: ...

    @abstractmethod
    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        """Identities linked to this playlist's events with match_status=PENDING
        and their log_artist already resolved."""
        ...

    @abstractmethod
    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        """Identities linked to this playlist's events with embedding IS NULL."""
        ...

    @abstractmethod
    def update_match_status(
        self,
        id: UUID,
        status: MatchStatus,
        tier: MatchTier,
    ) -> None: ...

    @abstractmethod
    def update_embedding(self, id: UUID, embedding: list[float]) -> None: ...

    @abstractmethod
    def bulk_reject_by_artist(self, artist_id: UUID) -> None:
        """Set all identities for this artist to AUTO_REJECTED."""
        ...
```

- [ ] **Step 7: Create `backend/repositories/log_events.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import LogEvent


class LogEventRepository(ABC):
    @abstractmethod
    def create(self, event: LogEvent) -> LogEvent:
        """Insert or ignore on (identity_id, playlist_id, played_at) conflict."""
        ...

    @abstractmethod
    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]: ...
```

- [ ] **Step 8: Create `backend/repositories/artists.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import Artist


class ArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: Artist) -> Artist: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Artist | None: ...

    @abstractmethod
    def list_all(self) -> list[Artist]:
        """Return all artists for fuzzy-matching in artist_matching_service."""
        ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Artist]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

    @abstractmethod
    def mark_enhancement_failed(self, mbid: str, error: str) -> None: ...
```

- [ ] **Step 9: Create `backend/repositories/works.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import Work


class WorkRepository(ABC):
    @abstractmethod
    def upsert(self, work: Work) -> Work: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Work | None: ...

    @abstractmethod
    def get_by_artist(self, artist_id: str) -> list[Work]: ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Work]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

    @abstractmethod
    def update_embedding(self, mbid: str, embedding: list[float]) -> None: ...
```

- [ ] **Step 10: Create `backend/repositories/recordings.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import Recording


class RecordingRepository(ABC):
    @abstractmethod
    def upsert(self, recording: Recording) -> Recording: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Recording | None: ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> list[Recording]: ...

    @abstractmethod
    def update_embedding(self, mbid: str, embedding: list[float]) -> None: ...
```

- [ ] **Step 11: Create `backend/repositories/library_files.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import EnrichmentStatus
from backend.domain.models import LibraryFile


class LibraryFileRepository(ABC):
    @abstractmethod
    def upsert(self, file: LibraryFile) -> LibraryFile: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> LibraryFile | None: ...

    @abstractmethod
    def get_by_path(self, file_path: str) -> LibraryFile | None: ...

    @abstractmethod
    def get_by_recording(self, recording_id: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def update_recording_link(
        self,
        id: UUID,
        recording_id: str,
        enrichment_status: EnrichmentStatus,
    ) -> None: ...

    @abstractmethod
    def count_by_format(self) -> dict[str, int]: ...

    @abstractmethod
    def count_by_enrichment_status(self) -> dict[str, int]: ...
```

- [ ] **Step 12: Create `backend/repositories/library_quarantine.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import LibraryQuarantine


class LibraryQuarantineRepository(ABC):
    @abstractmethod
    def create(self, entry: LibraryQuarantine) -> LibraryQuarantine: ...

    @abstractmethod
    def list_all(self) -> list[LibraryQuarantine]: ...

    @abstractmethod
    def get_by_path(self, file_path: str) -> LibraryQuarantine | None: ...
```

- [ ] **Step 13: Create `backend/repositories/matches.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import Match


class MatchRepository(ABC):
    @abstractmethod
    def create(self, match: Match) -> Match: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> Match | None: ...

    @abstractmethod
    def get_by_artist(self, artist_id: UUID) -> Match | None: ...

    @abstractmethod
    def delete_for_identity(self, identity_id: UUID) -> None: ...
```

- [ ] **Step 14: Create `backend/repositories/song_masters.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import SongMaster


class SongMasterRepository(ABC):
    @abstractmethod
    def upsert(self, master: SongMaster) -> SongMaster: ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> SongMaster | None: ...

    @abstractmethod
    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        """Return auto-selected masters for the given work IDs (skip manual selections)."""
        ...
```

- [ ] **Step 15: Create `backend/repositories/format_overrides.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import FormatOverride


class FormatOverrideRepository(ABC):
    @abstractmethod
    def create(self, override: FormatOverride) -> FormatOverride: ...

    @abstractmethod
    def get(self, work_id: str, format_name: str) -> FormatOverride | None: ...

    @abstractmethod
    def list_by_work(self, work_id: str) -> list[FormatOverride]: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...
```

- [ ] **Step 16: Create `backend/repositories/global_mapping_rules.py`**

```python
from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import GlobalMappingRule


class GlobalMappingRuleRepository(ABC):
    @abstractmethod
    def list_ordered(self) -> list[GlobalMappingRule]:
        """Return all rules ORDER BY priority DESC. First match wins in callers."""
        ...

    @abstractmethod
    def create(self, rule: GlobalMappingRule) -> GlobalMappingRule: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...
```

- [ ] **Step 17: Create `backend/repositories/mb_cache.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import MbCache


class MbCacheRepository(ABC):
    @abstractmethod
    def get(self, cache_key: str) -> MbCache | None: ...

    @abstractmethod
    def set(self, cache: MbCache) -> None: ...

    @abstractmethod
    def delete_expired(self) -> int:
        """Delete all rows where expires_at < now(). Returns count deleted."""
        ...
```

- [ ] **Step 18: Create `backend/repositories/progress_tracking.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import ProgressTracking


class ProgressTrackingRepository(ABC):
    @abstractmethod
    def upsert(self, task: ProgressTracking) -> ProgressTracking: ...

    @abstractmethod
    def get_by_id(self, task_id: str) -> ProgressTracking | None: ...

    @abstractmethod
    def list_running(self) -> list[ProgressTracking]: ...

    @abstractmethod
    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        """Mark running tasks not updated in N minutes as FAILED. Returns count updated."""
        ...
```

- [ ] **Step 19: Create `backend/repositories/settings.py`**

```python
from abc import ABC, abstractmethod

from backend.domain.models import UserSetting


class SettingsRepository(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def get_all(self) -> dict[str, str]: ...
```

- [ ] **Step 20: Verify mypy**

```
uv run mypy --strict backend/repositories/
```

Expected: `Success: no issues found in N source files`

- [ ] **Step 21: Commit**

```bash
git add backend/repositories/
git commit -m "feat: 19 repository ABCs — interface definitions for all domain entities"
```

---

## Task 7: In-Memory Fakes

**Files:**
- Create: `tests/fakes/__init__.py`
- Create: `tests/fakes/stations.py` through `tests/fakes/settings.py` (19 files)

- [ ] **Step 1: Create `tests/fakes/__init__.py`** (empty)

```python
```

- [ ] **Step 2: Create `tests/fakes/stations.py`**

```python
from uuid import UUID
from backend.domain.models import Station
from backend.repositories.stations import StationRepository


class FakeStationRepository(StationRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Station] = {}

    def create(self, station: Station) -> Station:
        self._data[station.id] = station
        return station

    def get_by_id(self, id: UUID) -> Station | None:
        return self._data.get(id)

    def get_by_call_letters(self, call_letters: str) -> Station | None:
        return next((s for s in self._data.values() if s.call_letters == call_letters), None)

    def list_all(self) -> list[Station]:
        return list(self._data.values())

    def update(self, station: Station) -> Station:
        self._data[station.id] = station
        return station

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
```

- [ ] **Step 3: Create `tests/fakes/playlists.py`**

```python
from uuid import UUID
from backend.domain.models import Playlist
from backend.repositories.playlists import PlaylistRepository


class FakePlaylistRepository(PlaylistRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Playlist] = {}

    def create(self, playlist: Playlist) -> Playlist:
        self._data[playlist.id] = playlist
        return playlist

    def get_by_id(self, id: UUID) -> Playlist | None:
        return self._data.get(id)

    def get_by_content_hash(self, content_hash: str) -> Playlist | None:
        return next((p for p in self._data.values() if p.content_hash == content_hash), None)

    def list_by_station(self, station_id: UUID) -> list[Playlist]:
        return [p for p in self._data.values() if p.station_id == station_id]
```

- [ ] **Step 4: Create `tests/fakes/broadcast_days.py`**

```python
from datetime import date
from uuid import UUID, uuid4
from backend.domain.models import BroadcastDay
from backend.repositories.broadcast_days import BroadcastDayRepository


class FakeBroadcastDayRepository(BroadcastDayRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastDay] = {}

    def get_or_create(self, station_id: UUID, broadcast_date: date) -> BroadcastDay:
        existing = next(
            (d for d in self._data.values()
             if d.station_id == station_id and d.broadcast_date == broadcast_date),
            None,
        )
        if existing:
            return existing
        new = BroadcastDay(id=uuid4(), station_id=station_id, broadcast_date=broadcast_date)
        self._data[new.id] = new
        return new

    def get_dates_for_station(self, station_id: UUID) -> list[date]:
        return sorted(
            d.broadcast_date for d in self._data.values() if d.station_id == station_id
        )

    def get_by_id(self, id: UUID) -> BroadcastDay | None:
        return self._data.get(id)
```

- [ ] **Step 5: Create `tests/fakes/log_artists.py`**

```python
from uuid import UUID
from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogArtist
from backend.repositories.log_artists import LogArtistRepository


class FakeLogArtistRepository(LogArtistRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogArtist] = {}
        # playlist_id → set of artist_ids (simulates the JOIN through log_events)
        self._playlist_artists: dict[UUID, set[UUID]] = {}

    def register_playlist_artist(self, playlist_id: UUID, artist_id: UUID) -> None:
        """Test helper: record that an artist appears in a playlist."""
        self._playlist_artists.setdefault(playlist_id, set()).add(artist_id)

    def upsert(self, artist: LogArtist) -> LogArtist:
        existing = self.get_by_normalized_name(artist.normalized_name)
        if existing:
            return existing
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, id: UUID) -> LogArtist | None:
        return self._data.get(id)

    def get_by_normalized_name(self, normalized_name: str) -> LogArtist | None:
        return next(
            (a for a in self._data.values() if a.normalized_name == normalized_name), None
        )

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [
            a for a in self._data.values()
            if a.id in ids and a.match_status == MatchStatus.PENDING
        ]

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [a for a in self._data.values() if a.id in ids and a.embedding is None]

    def update_match_status(
        self, id: UUID, status: MatchStatus, tier: MatchTier | None = None
    ) -> None:
        if artist := self._data.get(id):
            artist.match_status = status
            if tier is not None:
                pass  # log_artists has no match_tier column

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        if artist := self._data.get(id):
            artist.embedding = embedding
```

- [ ] **Step 6: Create `tests/fakes/log_identities.py`**

```python
from uuid import UUID
from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogIdentity
from backend.repositories.log_identities import LogIdentityRepository


class FakeLogIdentityRepository(LogIdentityRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogIdentity] = {}
        self._playlist_identities: dict[UUID, set[UUID]] = {}

    def register_playlist_identity(self, playlist_id: UUID, identity_id: UUID) -> None:
        self._playlist_identities.setdefault(playlist_id, set()).add(identity_id)

    def upsert(self, identity: LogIdentity) -> LogIdentity:
        existing = self.get_by_signature(identity.normalized_signature)
        if existing:
            return existing
        self._data[identity.id] = identity
        return identity

    def get_by_id(self, id: UUID) -> LogIdentity | None:
        return self._data.get(id)

    def get_by_signature(self, normalized_signature: str) -> LogIdentity | None:
        return next(
            (i for i in self._data.values()
             if i.normalized_signature == normalized_signature), None
        )

    def get_for_artist(self, artist_id: UUID) -> list[LogIdentity]:
        return [i for i in self._data.values() if i.artist_id == artist_id]

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        ids = self._playlist_identities.get(playlist_id, set())
        return [
            i for i in self._data.values()
            if i.id in ids and i.match_status == MatchStatus.PENDING
        ]

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        ids = self._playlist_identities.get(playlist_id, set())
        return [i for i in self._data.values() if i.id in ids and i.embedding is None]

    def update_match_status(self, id: UUID, status: MatchStatus, tier: MatchTier) -> None:
        if identity := self._data.get(id):
            identity.match_status = status
            identity.match_tier = tier

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        if identity := self._data.get(id):
            identity.embedding = embedding

    def bulk_reject_by_artist(self, artist_id: UUID) -> None:
        for identity in self._data.values():
            if identity.artist_id == artist_id:
                identity.match_status = MatchStatus.AUTO_REJECTED
                identity.match_tier = MatchTier.UNKNOWN
```

- [ ] **Step 7: Create `tests/fakes/log_events.py`**

```python
from uuid import UUID
from backend.domain.models import LogEvent
from backend.repositories.log_events import LogEventRepository


class FakeLogEventRepository(LogEventRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogEvent] = {}

    def create(self, event: LogEvent) -> LogEvent:
        key = (event.identity_id, event.playlist_id, event.played_at)
        existing = next(
            (e for e in self._data.values()
             if (e.identity_id, e.playlist_id, e.played_at) == key), None
        )
        if existing:
            return existing
        self._data[event.id] = event
        return event

    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]:
        return [e for e in self._data.values() if e.playlist_id == playlist_id]

    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]:
        return [e for e in self._data.values() if e.identity_id == identity_id]
```

- [ ] **Step 8: Create `tests/fakes/artists.py`**

```python
from backend.domain.models import Artist
from backend.repositories.artists import ArtistRepository


class FakeArtistRepository(ArtistRepository):
    def __init__(self) -> None:
        self._data: dict[str, Artist] = {}

    def upsert(self, artist: Artist) -> Artist:
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, mbid: str) -> Artist | None:
        return self._data.get(mbid)

    def list_all(self) -> list[Artist]:
        return list(self._data.values())

    def list_needing_enhancement(self) -> list[Artist]:
        return [a for a in self._data.values() if a.needs_enhancement]

    def mark_enhanced(self, mbid: str) -> None:
        if artist := self._data.get(mbid):
            artist.needs_enhancement = False

    def mark_enhancement_failed(self, mbid: str, error: str) -> None:
        if artist := self._data.get(mbid):
            artist.enhancement_error = error
```

- [ ] **Step 9: Create `tests/fakes/works.py`**

```python
from backend.domain.models import Work
from backend.repositories.works import WorkRepository


class FakeWorkRepository(WorkRepository):
    def __init__(self) -> None:
        self._data: dict[str, Work] = {}

    def upsert(self, work: Work) -> Work:
        self._data[work.id] = work
        return work

    def get_by_id(self, mbid: str) -> Work | None:
        return self._data.get(mbid)

    def get_by_artist(self, artist_id: str) -> list[Work]:
        return [w for w in self._data.values() if w.artist_id == artist_id]

    def list_needing_enhancement(self) -> list[Work]:
        return [w for w in self._data.values() if w.needs_enhancement]

    def mark_enhanced(self, mbid: str) -> None:
        if work := self._data.get(mbid):
            work.needs_enhancement = False

    def update_embedding(self, mbid: str, embedding: list[float]) -> None:
        if work := self._data.get(mbid):
            work.embedding = embedding
```

- [ ] **Step 10: Create `tests/fakes/recordings.py`**

```python
from backend.domain.models import Recording
from backend.repositories.recordings import RecordingRepository


class FakeRecordingRepository(RecordingRepository):
    def __init__(self) -> None:
        self._data: dict[str, Recording] = {}

    def upsert(self, recording: Recording) -> Recording:
        self._data[recording.id] = recording
        return recording

    def get_by_id(self, mbid: str) -> Recording | None:
        return self._data.get(mbid)

    def get_by_work(self, work_id: str) -> list[Recording]:
        return [r for r in self._data.values() if r.work_id == work_id]

    def update_embedding(self, mbid: str, embedding: list[float]) -> None:
        if rec := self._data.get(mbid):
            rec.embedding = embedding
```

- [ ] **Step 11: Create `tests/fakes/library_files.py`**

```python
from uuid import UUID
from backend.domain.enums import EnrichmentStatus
from backend.domain.models import LibraryFile
from backend.repositories.library_files import LibraryFileRepository


class FakeLibraryFileRepository(LibraryFileRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LibraryFile] = {}

    def upsert(self, file: LibraryFile) -> LibraryFile:
        existing = self.get_by_path(file.file_path)
        if existing:
            self._data[existing.id] = file
            return file
        self._data[file.id] = file
        return file

    def get_by_id(self, id: UUID) -> LibraryFile | None:
        return self._data.get(id)

    def get_by_path(self, file_path: str) -> LibraryFile | None:
        return next((f for f in self._data.values() if f.file_path == file_path), None)

    def get_by_recording(self, recording_id: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.recording_id == recording_id]

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.artist_mbid == artist_mbid]

    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]:
        return [
            f for f in self._data.values()
            if f.release_mbid == release_mbid
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]:
        return [
            f for f in self._data.values()
            if f.recording_mbid == recording_mbid
            and f.release_mbid is None
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def update_recording_link(
        self, id: UUID, recording_id: str, enrichment_status: EnrichmentStatus
    ) -> None:
        if f := self._data.get(id):
            f.recording_id = recording_id
            f.enrichment_status = enrichment_status

    def count_by_format(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._data.values():
            counts[f.format] = counts.get(f.format, 0) + 1
        return counts

    def count_by_enrichment_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._data.values():
            key = f.enrichment_status.value
            counts[key] = counts.get(key, 0) + 1
        return counts
```

- [ ] **Step 12: Create `tests/fakes/library_quarantine.py`**

```python
from backend.domain.models import LibraryQuarantine
from backend.repositories.library_quarantine import LibraryQuarantineRepository


class FakeLibraryQuarantineRepository(LibraryQuarantineRepository):
    def __init__(self) -> None:
        self._data: list[LibraryQuarantine] = []

    def create(self, entry: LibraryQuarantine) -> LibraryQuarantine:
        self._data.append(entry)
        return entry

    def list_all(self) -> list[LibraryQuarantine]:
        return list(self._data)

    def get_by_path(self, file_path: str) -> LibraryQuarantine | None:
        return next((e for e in self._data if e.file_path == file_path), None)
```

- [ ] **Step 13: Create `tests/fakes/matches.py`**

```python
from uuid import UUID
from backend.domain.models import Match
from backend.repositories.matches import MatchRepository


class FakeMatchRepository(MatchRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Match] = {}

    def create(self, match: Match) -> Match:
        self._data[match.id] = match
        return match

    def get_by_identity(self, identity_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.identity_id == identity_id), None)

    def get_by_artist(self, artist_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.artist_id == artist_id), None)

    def delete_for_identity(self, identity_id: UUID) -> None:
        to_delete = [id for id, m in self._data.items() if m.identity_id == identity_id]
        for id in to_delete:
            del self._data[id]
```

- [ ] **Step 14: Create `tests/fakes/song_masters.py`**

```python
from backend.domain.enums import SelectionMethod
from backend.domain.models import SongMaster
from backend.repositories.song_masters import SongMasterRepository


class FakeSongMasterRepository(SongMasterRepository):
    def __init__(self) -> None:
        self._data: dict[str, SongMaster] = {}  # keyed by work_id

    def upsert(self, master: SongMaster) -> SongMaster:
        self._data[master.work_id] = master
        return master

    def get_by_work(self, work_id: str) -> SongMaster | None:
        return self._data.get(work_id)

    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        return [
            m for work_id, m in self._data.items()
            if work_id in work_ids and m.selection_method == SelectionMethod.AUTO
        ]
```

- [ ] **Step 15: Create `tests/fakes/format_overrides.py`**

```python
from uuid import UUID
from backend.domain.models import FormatOverride
from backend.repositories.format_overrides import FormatOverrideRepository


class FakeFormatOverrideRepository(FormatOverrideRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, FormatOverride] = {}

    def create(self, override: FormatOverride) -> FormatOverride:
        self._data[override.id] = override
        return override

    def get(self, work_id: str, format_name: str) -> FormatOverride | None:
        return next(
            (o for o in self._data.values()
             if o.work_id == work_id and o.format_name == format_name), None
        )

    def list_by_work(self, work_id: str) -> list[FormatOverride]:
        return [o for o in self._data.values() if o.work_id == work_id]

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
```

- [ ] **Step 16: Create `tests/fakes/global_mapping_rules.py`**

```python
from uuid import UUID
from backend.domain.models import GlobalMappingRule
from backend.repositories.global_mapping_rules import GlobalMappingRuleRepository


class FakeGlobalMappingRuleRepository(GlobalMappingRuleRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, GlobalMappingRule] = {}

    def list_ordered(self) -> list[GlobalMappingRule]:
        return sorted(self._data.values(), key=lambda r: r.priority, reverse=True)

    def create(self, rule: GlobalMappingRule) -> GlobalMappingRule:
        self._data[rule.id] = rule
        return rule

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
```

- [ ] **Step 17: Create `tests/fakes/mb_cache.py`**

```python
from datetime import datetime, timezone
from backend.domain.models import MbCache
from backend.repositories.mb_cache import MbCacheRepository


class FakeMbCacheRepository(MbCacheRepository):
    def __init__(self) -> None:
        self._data: dict[str, MbCache] = {}

    def get(self, cache_key: str) -> MbCache | None:
        entry = self._data.get(cache_key)
        if entry and entry.expires_at > datetime.now(tz=timezone.utc):
            return entry
        return None

    def set(self, cache: MbCache) -> None:
        self._data[cache.cache_key] = cache

    def delete_expired(self) -> int:
        now = datetime.now(tz=timezone.utc)
        expired = [k for k, v in self._data.items() if v.expires_at <= now]
        for k in expired:
            del self._data[k]
        return len(expired)
```

- [ ] **Step 18: Create `tests/fakes/progress_tracking.py`**

```python
from datetime import datetime, timedelta, timezone
from backend.domain.enums import TaskStatus
from backend.domain.models import ProgressTracking
from backend.repositories.progress_tracking import ProgressTrackingRepository


class FakeProgressTrackingRepository(ProgressTrackingRepository):
    def __init__(self) -> None:
        self._data: dict[str, ProgressTracking] = {}

    def upsert(self, task: ProgressTracking) -> ProgressTracking:
        self._data[task.task_id] = task
        return task

    def get_by_id(self, task_id: str) -> ProgressTracking | None:
        return self._data.get(task_id)

    def list_running(self) -> list[ProgressTracking]:
        return [t for t in self._data.values() if t.status == TaskStatus.RUNNING]

    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=stale_threshold_minutes)
        count = 0
        for task in self._data.values():
            if task.status == TaskStatus.RUNNING and task.updated_at < cutoff:
                task.status = TaskStatus.FAILED
                count += 1
        return count
```

- [ ] **Step 19: Create `tests/fakes/settings.py`**

```python
from backend.domain.models import UserSetting
from backend.repositories.settings import SettingsRepository


class FakeSettingsRepository(SettingsRepository):
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = initial or {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def get_all(self) -> dict[str, str]:
        return dict(self._data)
```

- [ ] **Step 20: Write a smoke test verifying all fakes implement their ABCs**

Create `tests/test_fakes_implement_abcs.py`:

```python
"""Verify every fake is a concrete subclass of its ABC (raises TypeError at import if not)."""
import pytest

def test_station_fake_is_concrete() -> None:
    from tests.fakes.stations import FakeStationRepository
    repo = FakeStationRepository()
    assert repo is not None

def test_playlist_fake_is_concrete() -> None:
    from tests.fakes.playlists import FakePlaylistRepository
    assert FakePlaylistRepository() is not None

def test_broadcast_day_fake_is_concrete() -> None:
    from tests.fakes.broadcast_days import FakeBroadcastDayRepository
    assert FakeBroadcastDayRepository() is not None

def test_log_artist_fake_is_concrete() -> None:
    from tests.fakes.log_artists import FakeLogArtistRepository
    assert FakeLogArtistRepository() is not None

def test_log_identity_fake_is_concrete() -> None:
    from tests.fakes.log_identities import FakeLogIdentityRepository
    assert FakeLogIdentityRepository() is not None

def test_log_event_fake_is_concrete() -> None:
    from tests.fakes.log_events import FakeLogEventRepository
    assert FakeLogEventRepository() is not None

def test_artist_fake_is_concrete() -> None:
    from tests.fakes.artists import FakeArtistRepository
    assert FakeArtistRepository() is not None

def test_work_fake_is_concrete() -> None:
    from tests.fakes.works import FakeWorkRepository
    assert FakeWorkRepository() is not None

def test_recording_fake_is_concrete() -> None:
    from tests.fakes.recordings import FakeRecordingRepository
    assert FakeRecordingRepository() is not None

def test_library_file_fake_is_concrete() -> None:
    from tests.fakes.library_files import FakeLibraryFileRepository
    assert FakeLibraryFileRepository() is not None

def test_library_quarantine_fake_is_concrete() -> None:
    from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository
    assert FakeLibraryQuarantineRepository() is not None

def test_match_fake_is_concrete() -> None:
    from tests.fakes.matches import FakeMatchRepository
    assert FakeMatchRepository() is not None

def test_song_master_fake_is_concrete() -> None:
    from tests.fakes.song_masters import FakeSongMasterRepository
    assert FakeSongMasterRepository() is not None

def test_format_override_fake_is_concrete() -> None:
    from tests.fakes.format_overrides import FakeFormatOverrideRepository
    assert FakeFormatOverrideRepository() is not None

def test_global_mapping_rule_fake_is_concrete() -> None:
    from tests.fakes.global_mapping_rules import FakeGlobalMappingRuleRepository
    assert FakeGlobalMappingRuleRepository() is not None

def test_mb_cache_fake_is_concrete() -> None:
    from tests.fakes.mb_cache import FakeMbCacheRepository
    assert FakeMbCacheRepository() is not None

def test_progress_tracking_fake_is_concrete() -> None:
    from tests.fakes.progress_tracking import FakeProgressTrackingRepository
    assert FakeProgressTrackingRepository() is not None

def test_settings_fake_is_concrete() -> None:
    from tests.fakes.settings import FakeSettingsRepository
    assert FakeSettingsRepository() is not None
```

- [ ] **Step 21: Run the smoke tests**

```
uv run pytest tests/test_fakes_implement_abcs.py -v
```

Expected: `18 passed`

- [ ] **Step 22: Run mypy on fakes**

```
uv run mypy --strict tests/fakes/
```

Expected: `Success: no issues found`

- [ ] **Step 23: Commit**

```bash
git add tests/fakes/ tests/test_fakes_implement_abcs.py
git commit -m "feat: 19 in-memory fakes implementing all repository ABCs"
```

---

## Task 8: Frontend Scaffold + Zod Schema Stubs

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/lib/schemas/*.ts` (10 files)

- [ ] **Step 1: Create `frontend/package.json`**

```json
{
  "name": "retrostation-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "@tanstack/react-query": "^5.0.0",
    "react-router-dom": "^7.0.0",
    "zod": "^3.0.0",
    "lucide-react": "^0.400.0",
    "@tanstack/react-virtual": "^3.0.0",
    "zustand": "^5.0.0"
  },
  "devDependencies": {
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "^4.0.0",
    "typescript": "^5.6.0",
    "vite": "^6.0.0",
    "tailwindcss": "^4.0.0",
    "@tailwindcss/vite": "^4.0.0"
  }
}
```

- [ ] **Step 2: Create `frontend/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Create `frontend/vite.config.ts`**

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
  },
})
```

- [ ] **Step 4: Create `frontend/index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>RetroStation</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 5: Create `frontend/src/main.tsx`** (minimal placeholder)

```tsx
import React from 'react'
import ReactDOM from 'react-dom/client'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <div>RetroStation — Phase 0 scaffold</div>
  </React.StrictMode>,
)
```

- [ ] **Step 6: Create `frontend/src/lib/schemas/stations.ts`**

```typescript
import { z } from 'zod'

export const StationSchema = z.object({})
export const StationListSchema = z.array(StationSchema)
export const StationDashboardSchema = z.object({})

export type Station = z.infer<typeof StationSchema>
export type StationDashboard = z.infer<typeof StationDashboardSchema>
```

- [ ] **Step 7: Create `frontend/src/lib/schemas/playlists.ts`**

```typescript
import { z } from 'zod'

export const PlaylistSchema = z.object({})
export const PlaylistEventSchema = z.object({})
export const ExportResultSchema = z.object({})

export type Playlist = z.infer<typeof PlaylistSchema>
export type PlaylistEvent = z.infer<typeof PlaylistEventSchema>
export type ExportResult = z.infer<typeof ExportResultSchema>
```

- [ ] **Step 8: Create `frontend/src/lib/schemas/library.ts`**

```typescript
import { z } from 'zod'

export const LibraryStatusSchema = z.object({})
export const LibraryFileSchema = z.object({})

export type LibraryStatus = z.infer<typeof LibraryStatusSchema>
export type LibraryFile = z.infer<typeof LibraryFileSchema>
```

- [ ] **Step 9: Create `frontend/src/lib/schemas/artists.ts`**

```typescript
import { z } from 'zod'

export const ArtistSchema = z.object({})
export const ArtistDetailSchema = z.object({})
export const ArtistSearchResultSchema = z.object({})

export type Artist = z.infer<typeof ArtistSchema>
export type ArtistDetail = z.infer<typeof ArtistDetailSchema>
export type ArtistSearchResult = z.infer<typeof ArtistSearchResultSchema>
```

- [ ] **Step 10: Create `frontend/src/lib/schemas/works.ts`**

```typescript
import { z } from 'zod'

export const WorkSchema = z.object({})
export const WorkFilesTableRowSchema = z.object({})

export type Work = z.infer<typeof WorkSchema>
export type WorkFilesTableRow = z.infer<typeof WorkFilesTableRowSchema>
```

- [ ] **Step 11: Create `frontend/src/lib/schemas/matches.ts`**

```typescript
import { z } from 'zod'

export const MatchSchema = z.object({})
export const MatchCandidateSchema = z.object({})

export type Match = z.infer<typeof MatchSchema>
export type MatchCandidate = z.infer<typeof MatchCandidateSchema>
```

- [ ] **Step 12: Create `frontend/src/lib/schemas/matcher.ts`**

```typescript
import { z } from 'zod'

export const MatcherQueueItemSchema = z.object({})
export const ArtistResolutionSchema = z.object({})

export type MatcherQueueItem = z.infer<typeof MatcherQueueItemSchema>
export type ArtistResolution = z.infer<typeof ArtistResolutionSchema>
```

- [ ] **Step 13: Create `frontend/src/lib/schemas/tasks.ts`**

```typescript
import { z } from 'zod'

export const ActiveTaskSchema = z.object({})
export const ProgressDataSchema = z.object({})

export type ActiveTask = z.infer<typeof ActiveTaskSchema>
export type ProgressData = z.infer<typeof ProgressDataSchema>
```

- [ ] **Step 14: Create `frontend/src/lib/schemas/settings.ts`**

```typescript
import { z } from 'zod'

export const SettingsSchema = z.object({})
export const PathConfigSchema = z.object({})

export type Settings = z.infer<typeof SettingsSchema>
export type PathConfig = z.infer<typeof PathConfigSchema>
```

- [ ] **Step 15: Create `frontend/src/lib/schemas/index.ts`**

```typescript
export * from './stations'
export * from './playlists'
export * from './library'
export * from './artists'
export * from './works'
export * from './matches'
export * from './matcher'
export * from './tasks'
export * from './settings'
```

- [ ] **Step 16: Install frontend dependencies**

```
cd frontend && npm install
```

Expected: installs all packages, no peer dependency errors.

- [ ] **Step 17: Verify TypeScript compiles**

```
cd frontend && npm run typecheck
```

Expected: no errors.

- [ ] **Step 18: Commit**

```bash
git add frontend/
git commit -m "feat: frontend scaffold — Vite + React 19 + Zod schema stubs for all 9 domains"
```

---

## Phase 0 Gate

Both of the following must pass before starting Phase 1:

**Backend:**
```
uv run pytest tests/integration/test_migrations.py tests/test_fakes_implement_abcs.py -v
uv run mypy --strict backend/ tests/fakes/
uv run ruff check backend/ tests/
```

**Frontend:**
```
cd frontend && npm run typecheck
```

All commands must exit with code 0 and zero errors/warnings.
