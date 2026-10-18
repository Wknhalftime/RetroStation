# RetroStation architecture (one page)

**Status: 2026-10-18.** Checked against master 260a04e plus D111 (feat/silent-files); the
"Tune-in cues" section was added 2026-10-17 with feat/cue-edits-data and extended with
feat/cue-editor-api, the editor API; the "Station liners" section was added 2026-10-18 with
feat/station-liners (comment audit: `audit/comment-audit.md`). Rulings cited here are in
`audit/rulings.jsonl` (AUD-R015..R031 ACTIVE). If the code and this page disagree, fix one of
them in the same PR.
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

## Derived counts kept by triggers

- `station_identity_plays` (migration 0040) is COUNT(*) of `play_events` per (playlist's
  station, identity). Triggers on `play_events` (insert / delete / update / truncate) and on
  `playlists` (delete, station change) keep it equal, so no service writes it and no task
  reconciles it. The station missing-matches report reads it instead of scanning every play.
- It is not a work list: no task reads it, so it is not in the status-column table below.
- The check is `tests/integration/test_station_identity_plays_pg.py`, which compares it with
  the count after every kind of write. This is the only trigger-maintained table; a second one
  copies this one's shape (statement-level triggers with transition tables).

## Tune-in cues: manual edits beat autocue

- `stream_cue_edits` (migration 0041) holds a song's manual cue markers, keyed by `audio_hash`
  like `stream_cues`. No task purges or prunes it: the analyser purge and the orphan prune
  delete only from `stream_cues`, so an edit is never lost to a re-analysis.
- Both radio cue reads prefer an edit over the autocue row: the day read and the per-song D85
  re-read in `db/repositories/playable_schedule.py`. The reader joins both rows by key and the
  domain's `edited_cues` decides.
- The checks are `tests/integration/test_stream_cue_edits_pg.py::test_purge_and_prune_leave_edits`
  and the reader tests in `tests/integration/test_playable_schedule_edits.py`.
- The editor API (`routers/library/cues.py` -> `services/streaming/cue_editor.py`) is the only
  writer of `stream_cue_edits`. Save refuses what the radio would skip by asking the domain's
  own `PlayableFile.is_playable` (D9): there is no second copy of the rule, so radio = preview.
  Change D9 or the reader and the editor follows.
- `/waveform` runs ffmpeg inside the API process, in a request thread, with no cache
  (`playout/peaks.py`, 200 peaks/s). `/audio` serves a library file by its id, never by a path.
- Its checks are `tests/services/streaming/test_cue_editor.py`,
  `tests/routers/test_cue_editor_routes.py` and `tests/playout/test_peaks.py`.

## Station liners: a per-station clip library, analysed like songs

- `station_liners` (migration 0042) holds one row per liner: name, `file_name`, `format`,
  `audio_hash`, `duration_ms`. `stations.songs_between_liners` is the cadence (NULL = off, LR1).
  The same audio is one liner per station (unique `(station_id, audio_hash)`); deleting a
  station cascades its rows. Nothing plays liners yet: the on-air side is a later PR (L2).
- Files live at `stream_work_dir/liners/<station_id>/<liner_id>.<format>`
  (`services/streaming/liners.py`); the service commits its own writes, then removes files.
  An upload holds one sync DB connection for its whole duration, as the sign-off upload does.
- A manual cue edit is three columns on the liner row (start, end of message, stop; the
  fade-out sits at the stop). It never goes to `stream_cue_edits`, which is per song hash
  (see Tune-in cues above), and an edit always beats autocue.
- Autocue touches liners in three places:
  - the run's priority set starts with the liners needing analysis, then today's songs
    (`cue_precompute`, `LinerCueWork`);
  - `store_if_current` accepts a liner id carrying the hash read before analysing, as it does
    a library file id;
  - the orphan prune treats a hash held by any liner as in use, so it never drops a liner's
    `stream_cues` row.
- The cue meter's processed count includes liners while its total counts library files only,
  so it can pass the total within a run (capped; resets next run).
- The checks are the liner PG integration tests (`tests/integration/*liner*`).

## Background work: an orchestrated command pipeline (not pub/sub)

- **Messages are commands.** Each Huey message names one consumer and carries only a scope (an id,
  a scope word, or nothing). Event graph 2026-10-09: 17 commands, 0 events, 0 queries, no cycles.
  One exception (AUD-R030): a matching stage of a re-check's run also carries the run's id
  (`artist_matching_task(playlist_id, run_id)` → `identity_matching_task(playlist_id, run_id)`),
  so it can report to the run's progress row. The id is optional: an upload's matching has none.
- **The work list lives in status columns**, not in the message. These columns are the contract
  between tasks (AUD-R015):

| column / predicate | written by | read by |
|---|---|---|
| `broadcast_artists` / `track_identities.match_status` (+ `reason_code` DEFERRED_RETRY, MB_UNAVAILABLE) | ingest, matchers, review UI, re-check rewind, migration 0036 (once, AUD-R025) | artist / identity matching; the re-check rewinds every MB_UNAVAILABLE song (AUD-R028) |
| `track_identities.rejected_file_ids` | Reject / Unmatch (API), library_files.merge_into | song matching skips those files and their current works |
| `library_files.indexed_at` / `missing_since` after the newest COMPLETED `matching_recheck` run's `started_at` (`progress_tracking`) | scan / watcher upsert and relocate (`indexed_at` moves only for a new, back-from-missing or size/mtime-changed file, AUD-R023 D11), `mark_missing` | targeted re-check (`rematch_undecided_task`) |
| `library_files.audio_hash IS NULL` | scan / watcher upsert | hash backfill, cue analysis |
| `library_files.enrichment_status` | scan / watcher, enrichment, retry button | library enrichment |
| `artists` / `works` / `recordings.needs_enhancement` | MusicBrainz upsert | MB enrichment |
| `artists` with `origin = 'local'`, no `mbid`, and `mb_lookup_at` NULL or older than a present file's `indexed_at` | grouping (`upsert_local_artist`), the linker's stamp (`mb_lookup_at` / `mb_lookup_outcome`, migration 0037, AUD-R026) | local-artist linking (`link_local_artists_task`) |
| `artists.mb_lookup_outcome = 'linked'` with `mb_lookup_at` after the newest COMPLETED `matching_recheck` run's `started_at` | the linker | targeted re-check: the second source of its wave's names (AUD-R026) |
| no `stream_cues` row for the file's hash (`stream_cue_work.NEEDS_ANALYSIS`), or for a station liner's hash (LR4) | cue upserts | cue analysis (liners first) |
| `stream_cues.audio_silent` (D111) | cue analysis (a silent verdict); migration `0039_stream_cues_audio_silent` deleted the failed rows and the floor rows once | schedule resolution (the song gets no file, so tune-in and the walk skip it); both M3U exports; the Library Silent Files report |
| `library_folder_staged_hashes` | watcher poll | watcher scan |
| the matching run's `progress_tracking` row (`matching`, `stages_left` / `stages_failed`, AUD-R030) | the re-check opens it; each matching stage refreshes it and finishes its stage; the last stage closes it | the matching stages, `/ws` |
| `progress_tracking` | every task envelope | `/ws` poll (one shared `ProgressFeed`, 0.5 s, only while a client listens), hash-backfill liveness; the daily prune drops finished rows after 14 days, except each type's newest completed row (the re-check watermark) |

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
    `identity_matching_task`. Every re-check with playlists to queue is one matching run: its
    stages carry the run id and report to one `matching` progress row (AUD-R030)
  - streaming no-cue report → `stream_cue_request_task`
- **Hand-offs:** every task-to-task hand-off goes through `tasks/_enqueue_chain.enqueue_or_log`
  (AUD-R012 (1), AUD-R014). The watcher poll also passes `on_failure` to release its staged
  folders, so a failed hand-off is retried on the next poll.
- **Periodic tasks reconcile** what a lost command leaves behind: hash-backfill resume and
  cue-analysis resume (every 5 min each), cue prune, progress prune (daily). Matching has no periodic reconcile: work a
  lost fan-out leaves pending waits for the next re-check (an MB pass or the Re-run Matching
  button), and so does a song a MusicBrainz outage deferred (MB_UNAVAILABLE), which every
  re-check rewinds (AUD-R028).
- **Every re-check's rewind** takes undecided artists (`needs_review`, `auto_rejected`) and
  undecided songs (those two plus `manual_rejected`): a song the curator rejected waits for
  the next run, then is re-scored without its rejected files. A manually rejected artist is
  never rewound (AUD-R029).
- **One worker per queue is a constraint, not a setting** (AUD-R017). Duplicate
  delivery is harmless only because jobs run one at a time and re-check their status column;
  a duplicated re-check repeats its rewind and fan-out, and matches and rejected artists are
  never touched.
  The matching workers' status writes are guarded on the `pending` status they read
  (`update_match_status_if_pending`, AUD-R018/R021), so a decision the user makes in the API
  while a run is in progress wins.
- **Lifecycle reporting** stays in per-task envelopes (AUD-R011, AUD-R012): `task_run` for the
  enrichment pair, the local-artist linking and the matching re-check, `task_failure_telemetry`,
  the tasks' own try/except, and `reported_failures`. No Huey signals (AUD-R016). A matching
  run's row spans many tasks (AUD-R030): `tasks/_matching_run` opens it from the re-check
  (`run_fan_out`) and reports each stage to it (`matching_stage`); the matching services only
  tick a `MatchingProgress` observer. Its writes are telemetry and never stop matching.

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
