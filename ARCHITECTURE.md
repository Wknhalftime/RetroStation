# RetroStation architecture (one page)

**Status: 2026-10-09.** Checked against master 78d0902 plus D15 (feat/local-artist-mbids)
(comment audit: `audit/comment-audit.md`). Rulings cited here are in `audit/rulings.jsonl`
(AUD-R015..R026 ACTIVE). If the code and this page disagree, fix one of them in the same PR.
Cite decisions by id and name, never by line number. Streaming decision ids (`D20`, `D109`,
...) come from `docs/superpowers/specs/2026-09-27-tune-in-streaming-design.md`, which is
gitignored, so the repo cannot resolve them; `D1`..`D8` in `audit/eda/` are the event-graph
spec's own, separate list.

## What it is

A single-user monolith. It imports radio station play logs (CSV), matches the plays to a local
music library enriched from MusicBrainz, and plays stations back as a radio stream through
Liquidsoap. Python 3.13 + FastAPI + PostgreSQL (pgvector), React + Vite frontend.

## Processes (`Procfile`)

| process | runs | job |
|---|---|---|
| `api` | `backend.run_server` | FastAPI with an async psycopg pool; `/ws` progress; launches Liquidsoap (`backend/playout`) |
| `worker` | Huey consumer `backend.tasks.huey_app.huey -w 1` | library scan, watcher, enrichment, ingest, matching |
| `cues` | Huey consumer `backend.tasks.cue_huey_app.cue_huey -w 1` | stream cue analysis, on its own queue so it never waits behind a scan |
| `web` | `npm run dev` in `frontend/` | UI |

All processes share one PostgreSQL database. The queues are SQLite files (`SqliteHuey`,
`results=False`). Tasks use sync psycopg; the API uses async psycopg.

## Layers (enforced by import-linter, `pyproject.toml [tool.importlinter]`)

```
main  ->  routers | tasks  ->  services  ->  db | playout  ->  repositories  ->  domain
```

- `domain/`: dataclasses split by subdomain (broadcast, catalog, library, matching, curation,
  system, streaming, tune_in). Stdlib only. **Enforced.**
- `repositories/`: ABC ports. `db/repositories/`: their Pg adapters. Services never import
  `backend.db`, except `services/repository_factory.py`. **Enforced.**
- `playout/`: takes primitives and imports nothing else from backend. **Enforced.**
- `domain.library` never references streaming (D20). **Enforced.**

**Known gaps** (true today, not rules to copy):
- Routers run SQL directly: 122 `.execute()` calls in 10 router files. `routers/matching.py` is
  1,081 lines and `routers/library/works.py` is 1,101.
- Wiring happens in three places (`main.py`, `dependencies.py`, `services/repository_factory.py`),
  not one.
- import-linter carries 7 baseline ignores.
- New code should not add to any of these.

## Background work: an orchestrated command pipeline (not pub/sub)

- **Messages are commands.** Each Huey message names one consumer and carries only a scope (an id,
  a scope word, or nothing). Event graph 2026-10-09: 17 commands, 0 events, 0 queries, no cycles.
- **The work list lives in status columns**, not in the message. These columns are the contract
  between tasks (AUD-R015):

| column / predicate | written by | read by |
|---|---|---|
| `broadcast_artists` / `track_identities.match_status` (+ `reason_code` DEFERRED_RETRY) | ingest, matchers, review UI, re-check rewind, migration 0036 (once, AUD-R025) | artist / identity matching |
| `track_identities.rejected_file_ids` | Reject / Unmatch (API), library_files.merge_into | song matching skips those files and their current works |
| `library_files.indexed_at` / `missing_since` after the newest COMPLETED `matching_recheck` run's `started_at` (`progress_tracking`) | scan / watcher upsert and relocate (`indexed_at` moves only for a new, back-from-missing or size/mtime-changed file, AUD-R023 D11), `mark_missing` | targeted re-check (`rematch_undecided_task`) |
| `library_files.audio_hash IS NULL` | scan / watcher upsert | hash backfill, cue analysis |
| `library_files.enrichment_status` | scan / watcher, enrichment, retry button | library enrichment |
| `artists` / `works` / `recordings.needs_enhancement` | MusicBrainz upsert | MB enrichment |
| `artists` with `origin = 'local'`, no `mbid`, and `mb_lookup_at` NULL or older than a present file's `indexed_at` | grouping (`upsert_local_artist`), the linker's stamp (`mb_lookup_at` / `mb_lookup_outcome`, migration 0037, AUD-R026) | local-artist linking (`link_local_artists_task`) |
| `artists.mb_lookup_outcome = 'linked'` with `mb_lookup_at` after the newest COMPLETED `matching_recheck` run's `started_at` | the linker | targeted re-check: the second source of its wave's names (AUD-R026) |
| no `stream_cues` row for the file's hash (`stream_cue_work.NEEDS_ANALYSIS`) | cue upserts | cue analysis |
| `library_folder_staged_hashes` | watcher poll | watcher scan |
| `progress_tracking` | every task envelope | `/ws` poll (0.5 s), hash-backfill liveness |

- **Flows:**
  - upload → `ingestion_task` → `artist_matching_task` → `identity_matching_task`, with
    `ingestion_task` → `embedding_task` as a side branch that hands off to nothing (AUD-R020)
  - `scan_library` → `library_scan_task` → hash backfill + `library_enrichment_task` →
    `mb_enrichment_task` → `link_local_artists_task` (AUD-R026), then
    `rematch_undecided_task("changed")` → `artist_matching_task` per playlist with pending
    work → `identity_matching_task` (AUD-R022 D1). The MB pass queues both; on `-w 1` the
    re-check runs after the linking, and its wave also holds the names just linked.
  - watcher poll (every 4 min) → `library_scan_files_task` → `library_enrichment_task` →
    `mb_enrichment_task` → `link_local_artists_task`, then `rematch_undecided_task("changed")`
    → (as above)
  - Re-run Matching (`POST /matching/run`, 202 `{"queued": true}`) →
    `rematch_undecided_task("all")` → `artist_matching_task` per playlist with pending work →
    `identity_matching_task`
  - streaming no-cue report → `stream_cue_request_task`
- **Hand-offs:** every task-to-task hand-off goes through `tasks/_enqueue_chain.enqueue_or_log`
  (AUD-R012 (1), AUD-R014). The watcher poll also passes `on_failure` to release its staged
  folders, so a failed hand-off is retried on the next poll.
- **Periodic tasks reconcile** what a lost command leaves behind: hash-backfill resume and
  cue-analysis resume (every 5 min each), cue prune. Matching has no periodic reconcile: work a
  lost fan-out leaves pending waits for the next re-check (an MB pass or the Re-run Matching
  button).
- **One worker per queue is a constraint, not a setting** (AUD-R017). Duplicate
  delivery is harmless only because jobs run one at a time and re-check their status column;
  a duplicated re-check repeats its rewind and fan-out, and decided items are never touched.
  The matching workers' status writes are guarded on the `pending` status they read
  (`update_match_status_if_pending`, AUD-R018/R021), so a decision the user makes in the API
  while a run is in progress wins.
- **Lifecycle reporting** stays in per-task envelopes (AUD-R011, AUD-R012): `task_run` for the
  enrichment pair, the local-artist linking and the matching re-check, `task_failure_telemetry`,
  the tasks' own try/except, and `reported_failures`. No Huey signals (AUD-R016).

## What is enforced in CI (`.github/workflows/ci.yml`)

- ruff check
- ruff format
- import-linter
- `mypy backend --strict`
- pytest with branch coverage ≥ 80%
- frontend typecheck, tests and build

Frozen acceptance tests are guarded by a local hook. The event graph (AUD-R019) is checked in
CI and in pre-commit: `uv run poe events-check` fails on any task hand-off hit or unresolved
dispatch that is not in `audit/event-graph.baseline.json`. A PR that adds one refreshes the
baseline with `uv run poe events-baseline` and says why.

## Rules for changing the system (agreed 2026-10-05)

1. **One way to do each thing.** Copy the existing pattern (envelope, hand-off helper,
   repository port). If you need a second pattern, change this page first.
2. **A rule exists only if a check enforces it.** An unenforced rule gets deleted, not kept as
   aspiration.
3. **Decisions live in `audit/rulings.jsonl`.** A ruling left PROPOSED for more than a week is
   treated as rejected.
