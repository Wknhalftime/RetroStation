# RetroStation architecture (one page)

**Status: 2026-10-18.** Checked against master 260a04e plus D111 (feat/silent-files); the
"Tune-in cues" section was added 2026-10-17 with feat/cue-edits-data and extended with
feat/cue-editor-api, the editor API; the "Station liners" section was added 2026-10-18 with
feat/station-liners, "Liners on air" with feat/liners-on-air, and liner cue editing with
feat/liner-cues (comment audit: `audit/comment-audit.md`); "Retired" and "Library works
writes" were added 2026-10-31 with the artist model's phase 0c. Rulings cited here are in
`audit/rulings.jsonl` (AUD-R015..R057 ACTIVE). If the code and this page disagree, fix one of
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
- Routers run SQL directly: 57 `.execute()` lines in 10 router files (phase 0c moved the works
  router's writes out, below). `routers/matching.py` is 1,109 lines.
- Wiring happens in three places (`main.py`, `dependencies.py`, `services/repository_factory.py`),
  not one.
- import-linter carries 7 baseline ignores.
- New code should not add to any of these.

## Library works writes: one sync service (phase 0c, C75)

- The works router's write routes (merge, split, reassign, format override create and delete,
  Set and Remove master) are plain `def` routes on `SyncRepos`: one sync connection and one
  transaction per request, committed when the route succeeds and rolled back when it raises.
  Set master calls `master_choice_service`; every other write calls
  `services/work_curation_service.py`, which commits nothing of its own.
- The service writes through `WorkCurationRepos`: the existing works, library file, recording,
  song master, format override, match and artist repositories wherever one already runs the
  statement, and `WorkCurationRepository` (`repositories/work_curation.py`, adapter
  `db/repositories/work_curation.py`) for the statements none of them has, moved from the
  router unchanged. Its fake
  (`tests/fakes/work_curation.py`) works on the other fakes' rows, and
  `tests/repositories/test_work_curation_contract.py` runs the same cases on the fake and on
  PostgreSQL.
- Behaviour is the router's, oddities included: a merge drops a source's hand-picked master,
  and the master recalculation replaces a manual pick that is no longer a present file of the
  work (phase 3b changes both). Each refusal is a `WorkCurationError` whose message is the
  route's detail.
- The router's read route (the work detail) keeps its SQL on the async connection until 3b
  rebuilds the Library pages (AUD-R010 noted).
- The checks are `tests/test_works_router_writes_have_no_sql.py` (no write route reaches an
  `.execute` or a psycopg or `backend.db` name),
  `tests/routers/test_works_write_routes_are_sync.py`,
  `tests/routers/test_works_write_refusals.py` and
  `tests/services/test_work_curation_service.py`.

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
  writer of `stream_cue_edits`. For a song, Save refuses what the radio would skip by asking
  the domain's own `PlayableFile.is_playable` (D9): there is no second copy of the song rule,
  so radio = preview. Change D9 or the reader and the editor follows. Liners are edited
  elsewhere (Station liners below).
- `/waveform` runs ffmpeg inside the API process, in a request thread, with no cache
  (`playout/peaks.py`, 200 peaks/s), for a library file and for a liner alike. `/audio` serves
  a library file by its id, and a liner's `/audio` serves the liner by its id; never by a path.
  The response models both editors share are in `routers/cue_schemas.py`.
- Its checks are `tests/services/streaming/test_cue_editor.py`,
  `tests/routers/test_cue_editor_routes.py` and `tests/playout/test_peaks.py`.

## Station liners: a per-station clip library, analysed like songs

- `station_liners` (migration 0043) holds one row per liner: name, `file_name`, `format`,
  `audio_hash`, `duration_ms`. `stations.songs_between_liners` is the cadence (NULL = off, LR1).
  The same audio is one liner per station (unique `(station_id, audio_hash)`); deleting a
  station cascades its rows. Liners on air are the next section.
- Files live at `stream_work_dir/liners/<station_id>/<liner_id>.<format>`
  (`services/streaming/liners.py`); the service commits its own writes, then removes files.
  After each add and delete it sweeps the station's folder: orphans older than an hour (one
  stored-name read) and stale `.partial` files go; failures are logged, never raised.
  An upload holds one sync DB connection for its whole duration, as the sign-off upload does.
- A manual cue edit is three columns on the liner row (start, end of message, stop; the
  fade-out sits at the stop). It never goes to `stream_cue_edits`, which is per song hash
  (see Tune-in cues above), and an edit always beats autocue.
- Liner cue edits live on that liner row and are edited only through
  `/stations/{id}/liners/{id}/cues` (`routers/station_liners.py` ->
  `services/streaming/liner_cues.py`; GET, PUT, DELETE, plus `/waveform`), never in
  `stream_cue_edits`: `LinerCuePorts` has no port to it, and every route reaches the liner
  through its own station (`owned_liner`), so another station's liner is a 404. Save refuses
  what `liner_points` would not play (a silent liner) and a Stop past the end; the radio reads
  the same row through `liner_points`. Its checks are
  `tests/services/streaming/test_liner_cue_editor.py` and
  `tests/routers/test_liner_cue_routes.py`.
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

## Liners on air: a third kind of stream item, timeless

- `StreamService.item` hands out three kinds of item: a song (`Assigned`), the sign-off
  (`FinalClip`) and a liner (`LinerSlot`, plan note 8). The walk to the next song runs first,
  so the end of the schedule signs off and no liner plays right before it; then, if the seq
  before was a song and the cadence is due, the station's playable liners are read in a worker
  thread, on the cue re-read's short-lived connections and under its bounds (`BoundedReads`
  in `cue_reread.py`: 0.5 s, at most 2 at once, D86(c)), so a slow database never holds the
  slot near the engine's 10 s fetch timeout. A busy, late or failed read (`StreamReadError`,
  or a row the domain rejects) is a warning and the song plays; an empty library plays the
  song quietly. A liner that fails to load is skipped
  and still resets the count.
- The count and the rotation belong to the listener session, from tune-in: the cadence is read
  with the station at tune-in, the landing is song 1, and the shuffle (`LinerRotation`,
  `next_liner` in `domain/liners.py`) moves on only on the event loop, once the slot is stored.
  A reconnect starts both over.
- Liners are timeless to placement (`sessions.song_heard`): during a liner, the reconnect live
  position (D109) and the bookmark (`_left_at`) both land at the top of the song the stream
  walked to after the song before it (`LinerSlot.next_song`), so neither a D80 landing nor a
  D85 re-read can move that point. Only the freeze watchdog follows the liner's own
  span. A liner shows no title.
- The checks are `tests/services/streaming/test_liner_playout.py` (rig `liner_rig.py`),
  `test_liner_read_bounds.py` and `test_liner_walked_position.py` (rig `liner_reads_rig.py`).

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
| `broadcast_artists` / `track_identities.match_status` (+ `reason_code` DEFERRED_RETRY, MB_UNAVAILABLE) | ingest, matchers, review UI, re-check rewind, migration 0036 (once, AUD-R025) | artist / identity matching; the re-check rewinds every MB_UNAVAILABLE song (AUD-R028, extended by AUD-R042) |
| `track_identities.rejected_file_ids` | Reject / Unmatch (API), library_files.merge_into | song matching skips those files and their current works |
| `library_files.indexed_at` / `missing_since` after the newest COMPLETED `matching_recheck` run's `started_at` (`progress_tracking`) | scan / watcher upsert and relocate (`indexed_at` moves only for a new, back-from-missing or size/mtime-changed file, AUD-R023 D11), `mark_missing` | targeted re-check (`rematch_undecided_task`) |
| `library_files.audio_hash IS NULL` | scan / watcher upsert | hash backfill, cue analysis |
| `library_files.enrichment_status` | scan / watcher, enrichment, retry button | library enrichment |
| `artists` / `works` / `recordings.needs_enhancement` | MusicBrainz upsert | MB enrichment |
| `artists` with `origin = 'local'`, no `mbid`, and `mb_lookup_at` NULL or older than a present file's `indexed_at` | grouping (`upsert_local_artist`), the linker's stamp (`mb_lookup_at` / `mb_lookup_outcome`, migration 0037, AUD-R026, in part superseded by AUD-R036) | local-artist linking (`link_local_artists_task`) |
| `artists.mb_lookup_outcome = 'linked'` with `mb_lookup_at` after the newest COMPLETED `matching_recheck` run's `started_at` | the linker | targeted re-check: the second source of its wave's names (AUD-R026, in part superseded by AUD-R036) |
| no `stream_cues` row for the file's hash (`stream_cue_work.NEEDS_ANALYSIS`), or for a station liner's hash (LR4) | cue upserts | cue analysis (liners first) |
| `stream_cues.audio_silent` (D111) | cue analysis (a silent verdict); migration `0039_stream_cues_audio_silent` deleted the failed rows and the floor rows once | schedule resolution (the song gets no file, so tune-in and the walk skip it); both M3U exports; the Library Silent Files report |
| `library_folder_staged_hashes` | watcher poll | watcher scan |
| the matching run's `progress_tracking` row (`matching`, `stages_left` / `stages_failed`, AUD-R030) | the re-check opens it; each matching stage refreshes it and finishes its stage; the last stage closes it | the matching stages, `/ws` |
| `progress_tracking` | every task envelope | `/ws` poll (one shared `ProgressFeed`, 0.5 s, only while a client listens), hash-backfill liveness; the daily prune drops finished rows after 14 days, except each type's newest completed row (the re-check watermark) |

- **Flows:**
  - upload → `ingestion_task` → `artist_matching_task` → `identity_matching_task`, with
    `ingestion_task` → `embedding_task` as a side branch that hands off to nothing (AUD-R020)
  - `scan_library` → `library_scan_task` → hash backfill + `library_enrichment_task` →
    `mb_enrichment_task` → `link_local_artists_task` (AUD-R026, in part superseded by
    AUD-R036), then `rematch_undecided_task("changed")` → `artist_matching_task` per playlist
    with pending work → `identity_matching_task` (AUD-R022 D1; its D2 and D6 amended by
    AUD-R039 and AUD-R036). The MB pass queues both; on `-w 1` the
    re-check runs after the linking, and its wave also holds the names just linked.
  - watcher poll (every 4 min) → `library_scan_files_task` → `library_enrichment_task` →
    `mb_enrichment_task` → `link_local_artists_task`, then `rematch_undecided_task("changed")`
    → (as above)
  - Re-run Matching (`POST /matching/run`, 202 `{"queued": true}`) →
    `rematch_undecided_task("all")` → `artist_matching_task` per playlist with pending work →
    `identity_matching_task`. Every re-check with playlists to queue is one matching run: its
    stages carry the run id and report to one `matching` progress row (AUD-R030)
  - streaming no-cue report → `stream_cue_request_task`
  - the gate (`uv run poe gate --base <worktree>`, phase 0b; never the dev stack):
    `gate.py clone` copies the dev DB into the branch's `retrostation_bench_gate_<branch>_<hash>`;
    from the base tree, `migrate` and the four recorders (`airplay_snapshot.py`,
    `golden_responses.py`, `dry_run_matching.py --stage artist` and `--stage identity`);
    then `migrate`, each `run-task` (`call_local`, every hand-off stubbed, so nothing
    reaches a queue) and each apply script, and the four again, from the branch; then the
    four compares. Tools never
    touch the dev DB: `pg_dump` in `clone` is its only reader. Every tool refuses a database
    without "bench" and sets `STREAM_ENABLED=false` and its own Huey file
    (`PYTEST_XDIST_WORKER=gate`) before it loads backend code
    (`tests/scripts/test_gate.py::test_never_opens_another_dsn`). The recorders only read
    (READ ONLY transactions; the dry runs answer MusicBrainz from its cache alone), and
    `staging.py` runs the branch's API (port 8020) and worker (no periodic tasks) on a copy
- **Hand-offs:** every task-to-task hand-off goes through `tasks/_enqueue_chain.enqueue_or_log`
  (AUD-R012 (1), AUD-R014). The watcher poll also passes `on_failure` to release its staged
  folders, so a failed hand-off is retried on the next poll.
- **Periodic tasks reconcile** what a lost command leaves behind: hash-backfill resume and
  cue-analysis resume (every 5 min each), cue prune, progress prune (daily). Matching has no periodic reconcile: work a
  lost fan-out leaves pending waits for the next re-check (an MB pass or the Re-run Matching
  button), and so does a song a MusicBrainz outage deferred (MB_UNAVAILABLE), which every
  re-check rewinds (AUD-R028, extended by AUD-R042).
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

## Retired (C70, AUD-R045)

A retired name stays dead. The PR that retires one deletes every reader, writer, field, fake,
test and live mention, records a ruling ("retired in phase N, do not recreate") in
`audit/rulings.jsonl`, and adds a row here. `tests/test_retired_names.py` fails, naming the
ruling, when a row's pattern appears in `backend/`, `frontend/src/`, `scripts/` or `tests/`, on
this page outside the table, or in `.claude/CLAUDE.md` and `.claude/rules/` (local runs only:
`.claude/` is gitignored, so CI never sees them). It skips migrations,
`backend/db/rollback_*.sql`, a row's allowed files (exact paths) and the check itself, which
names 0c's retirements to prove the table holds them. `docs/` is never read: an
old plan keeps its text and gains a "Retired" note at the top. Each entry is a Python regular
expression in backticks (write `\|` for a pipe); use a pattern where a bare name would collide.

| Name or pattern | Replacement | Phase | PR | Ruling | Allowed files |
|---|---|---|---|---|---|
| `work_dedup_service` `dedupe_works` `plan_duplicate_merges` `merge_work_group` | none: works merge through the Library's merge route, and from 3b through one merge service (C58) | 0c | #212 | AUD-R047 | none |
| `part_split_service` `split_part_works` `plan_part_splits` `split_misplaced_parts` `PartSplitRepos` `PartSplitPlan` | none: grouping compares part numbers (`title_part_number`) | 0c | #212 | AUD-R048 | none |
| `case_duplicate_service` `repair_case_duplicates` `plan_case_duplicate_repairs` `apply_case_duplicate_repair` `CaseDuplicateRepair` | none: the scan recognises a case-only rename | 0c | #212 | AUD-R049 | none |
| `settle_misplaced_masters` `repair_misplaced_masters` | none: every move settles the work a file leaves (`move_file_to_work`) | 0c | #212 | AUD-R050 | none |
| `realign_match_works` `repair_match_works` | none: moves and grouping take a file's matches along (`matches.move_to_work`) | 0c | #212 | AUD-R051 | none |
| `works\.merge_into\b` `work_repo\.merge_into\b` `WorkRepository\.merge_into\b` `def merge_into\(self, target_id\b` | the Library's merge route; from 3b, one merge service (C58). `library_files.merge_into` stays | 0c | #212 | AUD-R052 | none |
| `_merge_recordings\b` | none: went with the works merge | 0c | #212 | AUD-R053 | none |
| `list_local_footprints` `WorkFootprint` | none | 0c | #212 | AUD-R054 | none |
| `get_case_duplicate_groups` | none | 0c | #212 | AUD-R055 | none |
| `list_work_ids_with_master_off_work` | none | 0c | #212 | AUD-R056 | none |
| `list_off_file_work` `MatchOffFileWork` | none | 0c | #212 | AUD-R057 | none |
