# RetroStation architecture (one page)

**Status: 2026-10-18.** Checked against master 260a04e plus D111 (feat/silent-files); the
"Tune-in cues" section was added 2026-10-17 with feat/cue-edits-data and extended with
feat/cue-editor-api, the editor API; the "Station liners" section was added 2026-10-18 with
feat/station-liners, "Liners on air" with feat/liners-on-air, and liner cue editing with
feat/liner-cues (comment audit: `audit/comment-audit.md`); "Retired" and "Library works
writes" were added 2026-10-31 with the artist model's phase 0c, "Log-name links" 2026-11-01
with its phase 1a, "Whole-name Reject and Unmatch" 2026-11-01 with its phase 1b, "Status from
links and suggestions" and "Placeholders are never artists" 2026-11-03 with its phase 1c.
Rulings cited
here are in `audit/rulings.jsonl` (AUD-R015..R070, R100 and R101 ACTIVE). If the code and this
page disagree, fix one of them in the same PR.
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
- Routers run SQL directly: 45 `.execute()` lines in 10 router files (phase 0c moved the works
  router's writes out, and phase 1a the artist Resolve and Unmatch, below).
  `routers/matching.py` is 1,030 lines.
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

## Log-name links: one table, one writer (phase 1a, C30)

- A log name's link to a catalog artist is a row of `log_name_links` (migration 0044, AUD-R035):
  one per (name, artist) in every state (`active`, `suggested`, `rejected`, so a rejected pair
  stays remembered), with its origin, a strength (0 to 100), versioned evidence and the credit
  columns 4.2 fills. Both keys are foreign keys with no cascade; `mbid` is the artist's MBID
  copied alongside and never read for matching.
- `LogNameLinkService` (`services/log_name_link_service.py`) is the only caller of the port
  `LogNameLinkRepository`, whose adapter is the only writer of the table; the rules for links
  live in the service, never in the port or its fake (`tests/fakes/log_name_links.py`). It
  writes in the caller's transaction. A match target (an MBID or an `artists.id`) names the
  artist with that id, else the one holding that MBID, else a new stub of that MBID: name and
  sort name the MBID, no normalized name, `needs_enhancement` set. An unnamed stub is left out
  of `GET /library/artists`, its search and `list_all()` (`name IS DISTINCT FROM mbid`).
- Enrichment names a stub (`_enhance_artist`, phase 1a PR 2, AUD-R059): its MBID is checked
  first (a malformed one, which Resolve lets through, is recorded in `enhancement_error` and
  never sent to MusicBrainz, not even by the lookup pre-pass), then it takes
  MusicBrainz's name, sort name and disambiguation, and the normalized name unless another
  artist already holds it (asked first; it stays NULL until 2a's C41). An answer with no name
  is recorded the same way. An artist that is no stub keeps its name.
  `scripts/name_stubs.py --dsn` names, on a gate copy, every artist whose name is still its MBID
  and has no recorded error (0044's stubs and any made since), one transaction each, with every
  hand-off stubbed.
- Writers: the artist matcher (`LogNameLinkService.write_plan`: the name's status, guarded,
  then each MusicBrainz hit into the catalog, then the link, origin automatic, or its
  suggestions; never over a manual or rejected pair, and a match or suggestion of a rejected
  pair is held back, below), Resolve (a manual link, which makes even a rejected pair active
  again; the name's other links stay), Reject and Unmatch (every link of the name becomes
  `rejected`, C19; none is deleted since phase 1b), and migration 0045 once (below). Resolve,
  Reject and Unmatch are sync routes on `SyncRepos` (C75) calling
  `services/artist_resolution_service.py`, so status, cascade and link commit together. The
  port's delete is the service's only through Not an artist's `unlink_keeping_rejected`
  (below), which writes the rejected pairs back.
- Readers: Tier 1 of song matching (the first active link, manual first, then strength, then
  the oldest; the MBID is the linked artist's current one; a name with only suggestions has
  none), the re-check wave's linked names (`normalized_names_linked_since`), the works merge,
  which keeps a local artist a link in any state points at (AUD-R035, which changes AUD-R004),
  and the Resolution Center queue's candidates (the suggested links, phase 1c S8).
- The artist rows of `matches` were copied once (0044), the rollback net of 1a and 1b, and
  migration 0046 dropped them in phase 1c with their column, its CHECK, UNIQUE, foreign key and
  index, 0044's stub list and C34's two `broadcast_artists` columns (M35, S10, S11): every match
  is a song's (`identity_id` NOT NULL; `Match` has no artist), and the match port reads no
  artist. `backend/db/rollback_0046_drop_artist_rows.sql` puts the column back with one row per
  v0 copy (`evidence.legacy`): each link's, in any state, and each link 0045 deleted, from
  `link_demotion_backup`, so that table must stay while 0046's rollback may run. It recreates
  0044's stub list empty, so 0044's rollback then keeps 0044's stubs as ordinary artists. The
  duplicates 0044 collapsed into one link are gone for good. **The rollbacks run newest
  first:** 0046's, then 0045's, then 0044's (phase 7's runbook); nothing enforces the order.
  0044's clears `LINK_REJECTED`, which the code before 1b cannot read, and leaves the name in
  review: at pending the old matcher could link again a rejected pair, since those go with the
  links.
- Known gap until 2a: a stub holds its MBID, so the local-artist linker records a local
  artist tagged with that MBID as `duplicate` instead of linking it, and enrichment files a
  work for it under the stub. The dev database had none of either at the copy (2026-11-01
  gate copy).
- The checks are `tests/test_log_name_links_single_writer.py`,
  `tests/integration/test_matches_song_only_pg.py` (0046 and its rollback),
  `tests/test_retired_names.py` (phase 1c's rows), `tests/services/test_tier1_link_read.py`,
  `tests/services/test_artist_link_writes.py`, `tests/routers/test_resolve_writes_link.py`,
  `tests/repositories/test_log_name_links_contract.py`, the migration's
  `tests/integration/test_log_name_links_*_pg.py` and the naming's
  `tests/tasks/test_enrichment_names_stubs.py` and `tests/tasks/test_enrichment_stub_checks.py`.

## Whole-name Reject and Unmatch: safe cascades (phase 1b, M24, AUD-R037)

- One pure function decides what a whole-name Reject or Unmatch does to the name's lines:
  `plan_cascade` (`domain/artist_cascade.py`). It keeps the manual lines (`manual_matched`,
  `manual_rejected`) and the lines matched to a same-name file (`auto_matched`, and the match
  `play_file_resolution` plays is to a file whose `normalized_artist_name` is the name's
  `normalized_name`, C16); every other line changes. A line is on air while it is
  `auto_matched` or `manual_matched`.
- Reject: the name `manual_rejected`, its links `rejected`, the changed lines `auto_rejected`
  (status only, as before). Unmatch: the name `needs_review` / `USER_UNMATCHED`, its links
  `rejected`, the changed lines `needs_review` / `USER_UNMATCHED` with their matches deleted.
  Kept lines are untouched. Each answers with the counts of what it did (`cascade`: lines and
  plays taken off air; lines kept on air, manual and same-name), read when it runs.
- `GET /matching/artists/{id}/cascade-preview?action=reject|unmatch` returns the same counts
  from the same plan and writes nothing; the artist panel shows them in a confirmation before
  either action, and the Resolution Center's banner shows the action's own counts after.
- The lines are read and written through `ArtistCascadeRepository` (`repositories/
  artist_cascade.py`, fake `tests/fakes/artist_cascade.py`, contract test
  `tests/repositories/test_artist_cascade_contract.py`). From phase 1c the name's status is
  written by `LogNameLinkService` (below) except Unmatch's decided-only write
  (`unmatch_if_decided`, the guard behind its 409); the resolution port's old status write and
  whole-name song statements retired in phase 1c (AUD-R065 to R068). The
  action reads the lines `FOR UPDATE`, so a matcher's guarded song write waits for it instead
  of being overwritten (AUD-R018).
- A rejected pair stays rejected (S4, C19): when the artist matcher's automatic match targets
  an artist whose link with the name is `rejected`, `hold_back_refused_link` turns it into
  `needs_review` / `LINK_REJECTED`, written through the same guarded write; no link, no
  catalog upsert, no cascade. The gate's dry run asks the same function. Only a manual
  Resolve makes the pair active again.
- The station missing-matches report counts a line by its own status (C37, AUD-R038): a
  matched line under an unresolved name still plays and is not missing.
- The checks are `tests/domain/test_artist_cascade.py`, `tests/routers/test_safe_reject.py`,
  `tests/routers/test_cascade_preview.py`, `tests/services/test_rejected_links_stay_rejected.py`,
  `tests/routers/test_missing_matches_by_line_status.py`, the contract test above, and the
  relocked `tests/routers/test_matching_unmatch.py` and
  `tests/integration/test_curation_cascades_pg.py`.

## Status from links and suggestions (phase 1c, M22, C35, C36, AUD-R038)

- A log name's status comes from its links, by one rule, `status_from_links`
  (`domain/log_name_status.py`), which `LogNameLinkService` applies in the same transaction as
  every link change it writes, in order: a user action writes the status it names (Resolve
  `manual_matched`, Reject `manual_rejected`, their old machine reason cleared; Unmatch keeps
  `unmatch_if_decided`); a rejected name keeps its status; an active manual link
  `manual_matched`; any active link `auto_matched`; a suggestion `needs_review` /
  `LINK_SUGGESTED`; else the writer's own answer (`LINK_REJECTED`, `DEFERRED_RETRY`); else a
  matched name left with no link `needs_review` / `LINK_REMOVED`; else the last writer's
  status stands (pending after the re-check's rewind, other review reasons).
- The interim artist matcher (until phase 4): only 95+ (`MB_AUTO_LINK_SCORE`) or an exact
  name pointing at one artist links (`_decide_artist_zone`, `NormalizationStrategy`). Everything
  else it would have decided is a suggestion: the two old lead rules, an exact name of several
  MBID artists (several local-only ones still link the first), and what was `AMBIGUOUS_GAP` or
  `LOW_CONFIDENCE`. A name holds at most 3 suggestions (`MAX_ARTIST_SUGGESTIONS`), best first,
  each at the floor `MIN_PRESENTATION_SCORE` (56, as songs) or over, as `suggested` links
  (origin automatic). A MusicBrainz suggestion's artist is upserted into the catalog first, as
  a MusicBrainz match's is, unless the upsert would give its MBID to a local-only artist of its
  name: then it names a stub, and its evidence keeps the hit's name for the panel. A suggestion
  starts no cascade, so by-name song matching goes on; a suggested name the matcher defers
  stays `LINK_SUGGESTED`. `DEFERRED_RETRY` and its cascade are unchanged until phase 4. Each
  answer is planned once (`plan_answer`, which resolves a MusicBrainz target as its upsert
  will, so a rejected pair is refused) and written once (`write_plan`); only a planned
  `DEFERRED_RETRY` defers the name's songs.
- Migration 0045 (once, AUD-R039: an exception to D2 of AUD-R022, as amended by AUD-R036)
  copied every link and name it
  changed into `link_demotion_backup` (kept until Lance accepts 1c), deleted the links to
  MusicBrainz placeholders and of listed names (C7), rejected any active link of a rejected
  name, demoted each active legacy fuzzy link (v0 tier normalization, strength under 95) to a
  suggestion when its old score is 56 or more and deleted it otherwise, and set each changed
  name's status by the rule. Lines and song matches were not touched. It also created
  `log_name_placeholders`, the station names that are programming, not performers (the next
  section). `backend/db/rollback_0045_link_demotion.sql` restores
  what still holds what 0045 wrote (a name a re-check rewound or re-suggested counts as
  unmoved), and puts any name left with a reason the code before 1c cannot read back to
  pending. Run it after 0046's rollback and before 0044's, never out of that order: nothing
  stops 0044's alone.
- The Resolution Center queue's `candidates` are the name's suggested links, typed
  (`SuggestedLink`, and the zod schema `useMatchingQueue` parses with), best first; Accept is
  the manual resolve by the artist's catalog id. A name in review with a suggestion is likely,
  so the default view shows it (a change may widen the curator's review, never hide it); a
  matched name's leftover suggestions do not make it likely. The song file search is not
  restricted to a suggestion.
- The checks are `tests/services/test_status_from_links.py`,
  `tests/integration/test_status_from_links_pg.py` (every row agrees),
  `tests/services/test_interim_artist_rules.py`, `tests/integration/test_link_demotion_pg.py`,
  `tests/integration/test_rollback_0045_after_matching_pg.py` and
  `tests/routers/test_queue_suggested_links.py`.

## Placeholders are never artists (phase 1c S5-S7, S9, C7, AUD-R037)

- Two kinds. MusicBrainz's special-purpose artists (`SPECIAL_PURPOSE_MBIDS`,
  `domain/catalog.py`: Various Artists, [unknown] and the rest) are refused by the link
  writer: `LogNameLinkService` writes no link of any state to one, by MBID or by the catalog
  artist holding it, and makes no stub; the matcher's plan leaves it out, and Resolve to one
  answers 422 (`PlaceholderArtistError`). The Library's artist list and search leave them out.
- And the not-an-artist list, `log_name_placeholders` (port `repositories/
  log_name_placeholders.py`, fake `tests/fakes/log_name_placeholders.py`), by exact cleaned
  name: 0045 seeded the five station names (SPOT BREAK, SPECIAL PROGRAMMING, SYNDICATED
  PROGRAM, VARIOUS, VARIOUS ARTISTS); "38 Special" and "Traffic" are real acts, so no pattern.
  A listed name keeps its status (S3: the list hides it). It is hidden from the Resolution
  Center queue and its counts, and its lines from the station missing-matches report (total,
  items, impact share). Resolve on it answers 409 (`ListedLogNameError`).
- No run matches a listed name or song-matches its lines, which keep their state (Lance,
  2026-11-03: a file named like a spot could otherwise reach the stream). The artist matcher
  runs no engine for a pending one (`listed_name_result`: `needs_review` / `NO_CANDIDATES`,
  no cascade, no MusicBrainz search). The artist task's DEFERRED_RETRY reset passes it by, the
  re-check's rewinds (both scopes, and the `MB_UNAVAILABLE` sweep) skip it and its lines in
  their SQL, and the song stage decides none of its lines (`lines_to_match`). The gate's dry
  runs ask the same functions: the artist dry run records the refusal, the identity dry run
  leaves the lines out. A refused placeholder answer is held back the same way
  (`hold_back_refused_link`, before a rejected pair's: `NO_CANDIDATES`). A name listed while a
  run is under way: the matcher reads the list again before it writes a link, and the song
  stage as it reaches each artist, so neither writes for it. The auto-reject cascade passes a
  listed name by. A pending line of a listed name is no pending work (the matching run's count
  and the re-check's playlists leave it out). The one-off `scripts/dry_run_title_scoring.py`
  (AUD-R100's trial) does not filter listed names.
- Not an artist: `POST /matching/artists/{id}/not-an-artist` lists the name (`curator`) and
  removes its active and suggested links; a rejected pair stays rejected, written back as it
  was (`unlink_keeping_rejected`), so a name listed by mistake keeps the curator's rejections.
  `DELETE` takes it off the list; the name takes the status its links give (a matched name
  with none: `LINK_REMOVED`). Both are sync routes on `SyncRepos` (C75), idempotent (E4). The
  panel asks first; the page hides the name at once and sends the POST only when its
  five-second Undo is over, so an Undo inside them writes nothing (leaving the page sends it
  then; a closed tab may drop it, which writes nothing). The Undo has a banner of its own, and
  each held name stays hidden until its listing is sent. After that the "Show placeholders"
  view (`/queue?placeholders=true`: every listed name, any status or bucket) has "Back to the
  queue", and no song decision. `DELETE` writes the status only for a name that was listed.
- Known gap: the song resolve route itself does not refuse a line of a listed name; only the
  page offers no song action for one.
- The checks are `tests/services/test_placeholder_links_refused.py`,
  `tests/services/test_placeholder_list_wiring.py`,
  `tests/repositories/test_log_name_placeholders_contract.py`,
  `tests/routers/test_not_an_artist.py`, `tests/routers/test_library_hides_placeholders.py`,
  `tests/scripts/test_dry_run_placeholders.py`,
  `tests/scripts/test_listed_names_not_song_matched.py`, and the panel's and page's cases in
  `ArtistPanel.test.tsx` and `MatcherBrowser.test.tsx`.

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

## One file, one airing: the radio never repeats a file back to back

- A track that holds two songs (Boston's "Foreplay/Long Time") can be matched to two
  consecutive plays. The radio airs the file once and skips the second play; the log, the
  matches and the reports are untouched.
- A play that does not air (no file, silent, too short) does not separate the two; a different
  song does. A liner never separates them either: the walk picks the next song before the
  liner cadence is checked.
- The rule lives in `domain/tune_in.py` (`_same_file`) and nowhere else. `next_item`, the
  resume walk and the clock tune-in all go through it, so a reconnect lands where an unbroken
  listener would be. A clock inside a skipped play lands inside the file's first play, counted
  from when that play was logged. The M3U exports do not apply the rule.
- The check is `tests/domain/test_tune_in_repeat_file.py`.

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
| `broadcast_artists` / `track_identities.match_status` (+ `reason_code` DEFERRED_RETRY, MB_UNAVAILABLE, LINK_REJECTED, LINK_SUGGESTED, LINK_REMOVED) | ingest, matchers (an artist's status from its links through `LogNameLinkService`, C36: a match held back on a rejected link is `needs_review` / LINK_REJECTED, phase 1b S4; an answer under 95, or an exact name of several artists, is LINK_SUGGESTED, phase 1c S4), review UI, re-check rewind, migrations 0036 (once, AUD-R025) and 0045 (once, AUD-R039) | artist / identity matching; the re-check rewinds every MB_UNAVAILABLE song (AUD-R028, extended by AUD-R042) |
| `track_identities.rejected_file_ids` | Reject / Unmatch (API), library_files.merge_into | song matching skips those files and their current works |
| `library_files.indexed_at` / `missing_since` after the newest COMPLETED `matching_recheck` run's `started_at` (`progress_tracking`) | scan / watcher upsert and relocate (`indexed_at` moves only for a new, back-from-missing or size/mtime-changed file, AUD-R023 D11), `mark_missing` | targeted re-check (`rematch_undecided_task`) |
| `library_files.audio_hash IS NULL` | scan / watcher upsert | hash backfill, cue analysis |
| `library_files.enrichment_status` | scan / watcher, enrichment, retry button | library enrichment |
| `artists` / `works` / `recordings.needs_enhancement` | MusicBrainz upsert; stub artists (migration 0044, the artist matcher and Resolve, phase 1a) | MB enrichment |
| `artists` with `origin = 'local'`, no `mbid`, and `mb_lookup_at` NULL or older than a present file's `indexed_at` | grouping (`upsert_local_artist`), the linker's stamp (`mb_lookup_at` / `mb_lookup_outcome`, migration 0037, AUD-R026, in part superseded by AUD-R036) | local-artist linking (`link_local_artists_task`) |
| `artists.mb_lookup_outcome = 'linked'` with `mb_lookup_at` after the newest COMPLETED `matching_recheck` run's `started_at` | the linker | targeted re-check: the second source of its wave's names, the artist's own and its active log-name links' (AUD-R026, in part superseded by AUD-R036) |
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
  never rewound (AUD-R029). A name held back on a rejected link (LINK_REJECTED) is rewound
  like any `needs_review` name and held back again until the curator decides it (1b S4); a
  suggested name (LINK_SUGGESTED) is rewound with its suggestions and, whatever the matcher
  answers short of a link, is in review with them again (1c S3).
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
   treated as rejected. AUD-R032 to AUD-R099 are reserved for the artist model, which reaches
   master in one merge and adds rulings in each phase, so the next ruling on master is AUD-R102.

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
| `artist_has_match` | `LogNameLinkService.is_linked`: a log-name link in any state keeps a local artist | 1a | #214 | AUD-R058 | none |
| `matches\.artist_id` `xor_match_target` `matches_artist_id_target_id_key` `idx_matches_artist\b` `check_log_name_links_copy` `_MATCHES_LINKS_SQL` | `log_name_links`; a match is a song's (`identity_id` NOT NULL, 0046) | 1c | #235 | AUD-R060 | `tests/integration/test_matches_song_only_pg.py` `tests/integration/test_curation_scenarios_pg.py` `tests/scripts/gate_world.py` |
| `MatchRepository(?:\([^)]*\))?\.get_by_artist` `match(?:_repo\|es)\.get_by_artist` `get_by_artist\(.*\) -> Match\b` | the name's links, through `LogNameLinkService`; `WorkRepository.get_by_artist` is another method and stays | 1c | #235 | AUD-R061 | `tests/matching_seed.py` |
| `artist_candidates` | the name's suggested links (the queue's `candidates`, 1c S8) | 1c | #235 | AUD-R062 | `tests/integration/test_matches_song_only_pg.py` `tests/routers/test_matching.py` `tests/routers/test_queue_suggested_links.py` |
| `resolved_artist_mbid` | none: read by nothing; a name's MBID is its linked artist's | 1c | #235 | AUD-R063 | `tests/integration/test_matches_song_only_pg.py` |
| `split_artist_string` | none: a name holding several performers gets one link each (M9), from evidence in phase 4 | 1c | #235 | AUD-R064 | none |
| `\breject_open_songs\b` | Reject's cascade (`plan_cascade`, phase 1b) | 1c | #235 | AUD-R065 | none |
| `\breopen_every_song\b` | Unmatch's cascade (`plan_cascade`, phase 1b) | 1c | #235 | AUD-R066 | none |
| `\bdelete_song_matches\b` | Unmatch's cascade, which deletes the changed lines' matches (phase 1b) | 1c | #235 | AUD-R067 | none |
| `resolution\.set_status\b` `ArtistResolutionRepository\.set_status\b` `def set_status\(self, broadcast_artist_id\b` | `LogNameLinkService` writes the status from the links (`status_from_links`, 1c S3) | 1c | #235 | AUD-R068 | `tests/services/test_status_from_links.py` |
| `links\.unlink_all\b` `LogNameLinkService\.unlink_all\b` `def unlink_all\(self, broadcast_artist_id\b` | `unlink_keeping_rejected` (Not an artist keeps the rejected pairs) | 1c | #235 | AUD-R069 | none |
| `link_copy_stubs` | none: 0046's rollback recreates it empty for 0044's rollback | 1c | #235 | AUD-R070 | `tests/integration/test_matches_song_only_pg.py` `tests/integration/test_log_name_links_copy_pg.py` |
