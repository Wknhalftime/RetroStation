# RetroStation architecture (one page)

**Status: 2026-10-18.** Checked against master 260a04e plus D111 (feat/silent-files); the
"Tune-in cues" section was added 2026-10-17 with feat/cue-edits-data and extended with
feat/cue-editor-api, the editor API; the "Station liners" section was added 2026-10-18 with
feat/station-liners, "Liners on air" with feat/liners-on-air, and liner cue editing with
feat/liner-cues (comment audit: `audit/comment-audit.md`); "Retired" and "Library works
writes" were added 2026-10-31 with the artist model's phase 0c, "Log-name links" 2026-11-01
with its phase 1a, "Whole-name Reject and Unmatch" 2026-11-01 with its phase 1b, "Status from
links and suggestions" and "Placeholders are never artists" 2026-11-03 with its phase 1c,
"Artist identity" 2026-11-05 with its phase 2a, whose second PR extended it 2026-11-06 and its
third (Merge) 2026-11-07.
Rulings cited
here are in `audit/rulings.jsonl` (AUD-R015..R073, R100, R101 and R102 ACTIVE). If the code and this
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
  system, streaming, tune_in, endings). Stdlib only. **Enforced.**
- `repositories/`: ABC ports. `db/repositories/`: their Pg adapters. Services never import
  `backend.db`, except `services/repository_factory.py`. **Enforced.**
- `playout/`: takes primitives and imports nothing else from backend. **Enforced.** One named
  exception: `playout/loudness.py` returns the domain's `LoudnessTail` (see Song endings).
- `domain.library` never references streaming (D20). **Enforced.**

**Known gaps** (true today, not rules to copy):
- Routers run SQL directly: 25 `.execute()` lines in 6 router files (phase 0c moved the works
  router's writes out, phase 1a the artist Resolve and Unmatch, and phase 3a the song Resolve,
  Reject and Unmatch, below). `routers/matching.py` is 1,154 lines.
- Wiring happens in three places (`main.py`, `dependencies.py`, `services/repository_factory.py`),
  not one.
- import-linter carries 7 baseline ignores.
- New code should not add to any of these.

## Library works writes: one sync service (phase 0c, C75)

- The works router's write routes (merge, split, reassign, format override create and delete,
  Set and Remove master, and the work page's Prefer this copy and Release, 3b PR 5a) are plain
  `def` routes on `SyncRepos`: one sync connection and one transaction per request, committed
  when the route succeeds and rolled back when it raises. Set master and Prefer this copy call
  `master_choice_service`; the merge calls `work_merge_service.guarded_merge` (3b PR 3, "Work
  merges: the guard" below), which wraps `merge_works`; every other write calls
  `services/work_curation_service.py`, which commits nothing of its own.
- The service writes through `WorkCurationRepos`: the existing works, library file, recording,
  song master, format override, match and artist repositories wherever one already runs the
  statement, and `WorkCurationRepository` (`repositories/work_curation.py`, adapter
  `db/repositories/work_curation.py`) for the statements none of them has, moved from the
  router unchanged. Its fake
  (`tests/fakes/work_curation.py`) works on the other fakes' rows, and
  `tests/repositories/test_work_curation_contract.py` runs the same cases on the fake and on
  PostgreSQL.
- Behaviour is the router's, oddities included: the master recalculation replaces a manual pick
  that is no longer a present file of the work (phase 3b keeps this). Each refusal is a
  `WorkCurationError` whose message is the route's detail.
- The merge's master (phase 3b PR 1: 3b SPEC S3, E1, D8-D11; AUD-R032's "puts a hand-picked
  master first"). Before 3b a merge dropped every source's master and re-picked the target's.
  Now:
  - a hand-picked master, the target's or a source's, beats an automatic one and stays
    hand-picked (D11);
  - a pick counts only while its file is a present file of the merged work (D10);
  - between picks, the work with more matched plays keeps its master (D9). Those are the plays
    of the lines whose view work (`play_file_resolution.work_id`) is that work, counted by
    `WorkCurationRepository.matched_plays` on the merge's connection before anything moves. A
    tie goes to the target, then to the source listed first;
  - with no valid pick, the recalculation re-picks the master from the merged work's present
    files, stored automatic, as before (D8). Plays keep no automatic master, because every
    matching run re-picks it (`tasks/identity_matching_tasks.py`).

  The fake's `matched_plays` returns nothing (the fakes hold no plays), so its check is
  `tests/integration/test_work_merge_service_pg.py`.
- A merge deletes a source's local artist only when it is left with no work, no log-name link in
  any state (AUD-R035, which changes AUD-R004), no artist merge on either side (phase 2a S11,
  "Artist identity" below) and no performance crediting it (3b D19, AUD-R078: the act still
  performs a copy of the merged work). AUD-R035's "may be deleted only when" still holds; this is one more
  condition, so no ruling changes.
- A split or reassign that empties a work (3b PR 4, D7, D16, D31) first points each match still
  on it at its file's work and deletes its file-less recordings, as before; then the one
  deletion rule (`delete_if_empty`, "The merge list" below) decides. A work an override, entry
  or match on the work still names stays, losing only a master left on another work's file.
- The read routes (the work detail and its versions) call `services/work_page_service.py` on
  the async connection, and their SQL is in `db/repositories/work_page.py` (3b PR 5a, "The work
  page" below), so the router holds none (AUD-R010 had left it there; AUD-R074 replaced it for
  the song routes, "Song matches: one writer" below).
- The checks are `tests/test_works_router_writes_have_no_sql.py` (no write route reaches an
  `.execute` or a psycopg or `backend.db` name),
  `tests/routers/test_works_write_routes_are_sync.py`,
  `tests/routers/test_works_write_refusals.py`,
  `tests/services/test_work_curation_service.py` and (the merge's master)
  `tests/integration/test_work_merge_service_pg.py`.

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
  MusicBrainz's name, sort name and disambiguation, and the normalized name whoever holds it
  (phase 2a, AUD-R072, "Artist identity" below). An answer with no name is recorded the same
  way. An artist that is no stub keeps its name.
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
  links. These three stop at 0047, which has no script (phase 2a D4, "Artist identity" below).
- Known gap: a stub holds its MBID, so the local-artist linker records a local artist tagged
  with that MBID as `duplicate` instead of linking it, and enrichment files a work for it under
  the stub. 2a doesn't close this: both artists stay, and the name gets a `different_acts`
  suggestion when the stub is named. Merge (phase 2a S11, "Artist identity" below) joins them on
  the curator's request. The dev database had none
  of either at the copy (2026-11-01 gate copy).
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

## Artist identity: an artist per MBID, a name shared (phase 2a, C26, C41, C43, C69, M33, AUD-R041, AUD-R072, AUD-R073)

Every MBID can be its own artist, so two acts called Alice Cooper stay two. Air play and the
grouping of existing works don't change (`upsert_from_mb` returns the row it finds); a work kept
its artist until 3b PR 5b, when its artists became its performances' credits (S7, AUD-R082).

- **The index (C41, migration 0047):** `idx_artists_norm_name` is
  `UNIQUE (normalized_name) WHERE mbid IS NULL`: one name-only artist per name, and any number
  with an MBID. `idx_artists_normalized_name` is a plain index, for what a partial one can't
  serve: `works.py`'s candidate join, which now sees the works of every artist of the name
  (M18), and the local upsert's count. `get_by_normalized_name` answers for the name-only artist.
- **The name's MBIDs (`name_mbids`):** the lowercased, well-formed, non-placeholder values of
  present files whose `artist_mbid` holds exactly one MBID. A joined value, a placeholder, a
  malformed value, a missing file and an untagged file don't count.
- **`upsert_musicbrainz_artist` (S6):** found by MBID, that artist. Otherwise, when the name's
  MBIDs are none or only this one (D5), the name-only artist takes the MBID in place. Otherwise
  a new artist, `ON CONFLICT (mbid)`. It never returns another MBID's artist.
- **`upsert_local_artist` (S7), for every file the scan groups:** none of the name, a new
  name-only artist. One, on a name holding at most one MBID, that artist. Several, or a name
  holding several MBIDs, the single name-only artist, created if absent, so the result doesn't
  depend on which file the scan reaches first. `_fuzzy_match_work` runs first, and a fuzzy match
  still joins its work.
- **The local-artist linker (S9):** a decision that would be `linked` on a name holding several
  MBIDs records `ambiguous`, and the name stays name-only. Its lookups run as before.
- **Stubs (S13, D2):** a stub MusicBrainz names takes its normalized name whoever holds it. A
  name-only holder stays as it is, the name gets one `different_acts` suggestion, and no link
  moves. A symbol-only name normalizes to nothing and stays NULL.
- **A log name's rejected name (D7):** `_resolve` reads ahead by S6's rule, so a match that
  would promote a rejected artist stays refused (`LINK_REJECTED`). `plan_answer` turns an
  automatic match to a different artist with the normalized name of an artist the log name
  rejected, name-only or holding an MBID, into a suggestion, and the name stays in review. A
  mapping rule's answer counts as automatic here. A rejected stub with no name yet protects
  nothing until enrichment names it. `scripts/dry_run_matching.py` asks
  `hold_back_refused_link`, never `plan_answer`, so it shows such a name as matched.
- **A file's every artist (S1, S2, D3):** `library_files.artist_mbids` holds each MBID of the
  artist-MBID tag (NULL until filled, `{}` when untagged), and `credit` holds them in order as
  `{name, mbid, join}`, with `name` null until named and `join` null when unknown. The name columns
  and the scalar `artist_mbid` don't change, and song matching doesn't read these until 4.3. The
  file upsert writes both only when the file's size or mtime changed or they are NULL, so a scan
  never replaces names the backfill looked up. `indexed_at` isn't the signal, because it also moves
  when a file returns from missing. A row upserted from its stored model carries neither, so it
  leaves them alone. The ID3 reader takes the recording MBID from the UFID frame when TXXX has none.
- **Pairing (S4, E2, M32, `services/artist_credit.py`):** pure, so the scan and the backfill share
  it. MBIDs split on the MBID pattern, each kept once at its first position, and names never split.
  A single MBID takes the whole `artist` text. On a file holding several, `artists[i]` goes with
  `mbid[i]`, a value holding no MBID keeps its place, and joining words come from locating each
  name, in order, in the printed `artist`. When the counts differ, the names stay null.
- **A scan can't name what its tags can't (D9):** it does no lookup, and once the backfill has
  finished, nothing writes those null names. Each artist still gets its name, because the sweep
  stubs the MBID and enrichment names the stub. The file's `credit` stays null until the file changes and its tags can pair, and
  3a reads a null name from the artist by MBID.
- **The backfill (S3, E3, E6, `tasks/artist_credit_backfill_tasks.py`):** a run-once task that fills
  both columns of every row with `raw_metadata`, whatever its status. Vorbis values come from
  `raw_metadata`. MP3s are re-read from disk, because a multi-value ID3 frame lost its separators
  there, and an absent or unreadable one is counted and skipped. A file holding several MBIDs that
  its tags can't name asks `mb_cache` first, expired or not (`MusicBrainzCacheListing`), then
  `lookup_recordings`, and when MusicBrainz now credits someone else the tags stand (M1). It then
  fills S13's stubs and each artist's own name, runs the sweep, adds aliases and tag spellings, and
  sets the marker `library.artist_credits_backfilled`, after which it does nothing. While a lookup
  fails, its file stays unwritten, and the sweep and the marker wait (AUD-R028, extended by
  AUD-R042). That run still ends COMPLETED, with `done: false` in its result, and nothing queues
  the next one. **It must run before scans resume after 0048:** a scan's sweep would stub the MBIDs
  whose names the backfill would have found, so they couldn't reach S6's promotion. A full scan does
  that to all of them, and a watcher batch to those of the files it re-reads.
- **The sweep (S14, D6, D10, `tasks/artist_identity_tasks.py`):** each real MBID of a name holding
  several gets an artist, with the spelling most of its files carry, and the name gets one
  `shared_name` suggestion listing the MBIDs and the titles two or more of them share. Each real
  MBID of a file holding several gets one too, named only by files holding several
  (`ArtistCreditFilesRepository`), through S6, or as a stub when those files don't name it. Any other
  single-MBID file gets none: the linker and enrichment promote as before. It reads
  `library_files` (one grouped read per half, then one read of which of those MBIDs have an artist),
  because a file with a work is never grouped again, and a fuzzy or shortcut join never reaches the
  upsert. It runs last in
  `_run_scan` and in the watcher's scan, once from the backfill, and as
  `artist_identity_sweep_task`, which the gate can run. It commits what is pending, then each name
  and each file's MBID alone: a failure on one is logged and rolled back, and the next sweep retries
  it. A failed read is logged and the scan succeeds, and so does a scan whose connection drops here,
  since its work is already committed. The sweep moves no work, so a new artist lists with 0 works
  until enrichment files a new MusicBrainz work under it (`upsert_from_mb`), and the artist page's
  fallback shows its files.
- **What D10 costs:** a single-MBID file never names an artist the files half makes, because that
  would promote a name-only artist from tags alone and skip the linker's name check (AUD-R026, in
  part superseded by AUD-R036). So an MBID its multi-MBID files don't name is a stub, even when a
  name-only artist's own files carry that MBID. Enrichment names the stub, S13's row says so, and
  the linker records `duplicate` until Merge joins them.
- **Further credits (S8, D5, AUD-R073):** enrichment upserts every credited artist, not just the
  first, and a placeholder further credit gets none. The first credit and the work stayed as
  before until 3b PR 5b, which ends that clause of AUD-R073 (AUD-R082: a work's artists are its
  performances' credits). `upsert_further_credit` is narrower than S6: the name-only artist
  takes the MBID only when the name's MBIDs are exactly this one. With none, a new artist goes beside it, because a
  further credit is new and no tag agrees.
- **Known gap, accepted (D8):** a credit naming the MBID a tag was merged into makes a second artist
  beside the name-only one, because the tag's MBID isn't the credit's. The linker then looks up the
  tag, gets the credit's MBID and records `duplicate` on every run, and no suggestion row says so.
  Phase 5's Artist decisions page lists artists the linker recorded as `duplicate`
  (`artists.mb_lookup_outcome`), and Merge can join them. Air play doesn't change.
- **`artist_names` (S10, C43, `ArtistNameRepository`):** every name of an artist, with its source:
  `musicbrainz_name`, `musicbrainz_alias` or `tag`. A log spelling has no source, so the table
  refuses it. Each write of `artists.name` adds a row: `upsert_local_artist`,
  `upsert_musicbrainz_artist` (the sweep passes `tag`), `upsert_further_credit`, the linker's link
  and stub naming. A name goes with its artist. The backfill adds what those writers never saw: an
  artist's own name unless one of them recorded it, cached aliases and the spellings `credit` holds,
  unnamed stubs included. A spelling S4 took from the cache counts as a tag spelling there, because
  `credit` doesn't record where a name came from.
- **`library_suggestions` (C21, design 6.10; `LibrarySuggestionRepository`):** one row per name
  and reason where no file is named. A repeat updates candidates and evidence, never `state`, so
  a name the curator kept separate never reopens, and the same write again writes nothing. A
  name that stops holding several keeps its row. Its page comes in phase 5; nothing merges
  automatically (M33).
- **Merge and Unmerge (S11, E4, E5, M33; `services/artist_merge_service.py`):** only the curator
  merges, from the artist page. `artist_merges` (migration 0049) holds one direct edge per merged
  artist: its primary, `merged_by` (only `curator`), when, and a reason. `POST
  /library/artists/{id}/merge` merges `{id}` into `primary_id`, and `DELETE` on the same path
  deletes its one edge. Both are sync routes on `SyncRepos` (C75) and answer 204, and a repeat
  changes nothing. A merge into the artist itself, one that would make a cycle, one with a
  special-purpose artist on either side, and one of an artist already merged elsewhere are refused
  (409). A merge into an artist that is itself merged is kept as made, and the group's primary is
  the root its chain of edges ends at (E4). Each Merge and Unmerge first locks `artist_merges`
  (`SHARE ROW EXCLUSIVE`, which blocks no read), so two merges never pass their checks at once: two
  crossing merges can't make a cycle, and a second merge of one artist sees the first's edge. Only
  the adapter, `PgArtistMergeRepository`, writes the table, and only its SQL follows an edge:
  `MERGE_ROOTS_CTE` walks every chain to its root, and `ARTIST_GROUP_SQL` serves `artist_group`
  and the artist page alike. A merge with an artist a work merge has just deleted is a 404.
- **What a merge shows:** the merged artist leaves the artist list and its search, and its page
  redirects to the root (`merged_into`). The root lists its `members`, each with the artist it was
  merged into directly, and shows the group's works, then each member's files that aren't on one of
  them, one entry per title by the member's MBID (2a's question A). An entry counts every file of
  its MBID and title, because it opens them all. The primary's own files show that way while it
  owns no work, as for any artist with no works. The list counts the group's works and files, and
  `?mbid=` finds the root for a member's MBID. Credits, log-name links, works and
  MusicBrainz lookups keep the real artist: a merged artist stays in the matcher's pool, and song
  matching reads the group only from 4.3, so neither action queues a re-check. A link to a merged
  artist shows under the primary once a page lists links (phase 5). The work merge never deletes an
  artist on either side of an edge, and 0049's foreign keys don't cascade. Nothing refuses a merge
  into an unnamed stub, which the page never offers; the group then leaves the list until
  enrichment names the stub.
- **Rollback (D4):** there is no script from 0047 on. The index can't go back while two artists
  share a name, and the code before 2a fails on every scan without it (`ON CONFLICT
  (normalized_name)` has no unique index), so the older scripts can't run past it. The rollback
  is the dump (phase 7 S5).
- The checks are `tests/integration/test_artist_name_index_pg.py`,
  `tests/integration/test_artist_merge_pg.py`,
  `tests/test_artist_merge_writers.py`,
  `frontend/src/pages/library/ArtistDetail.test.tsx`,
  `tests/integration/test_artist_identity_sweep_lost_connection_pg.py`,
  `tests/integration/test_artist_credit_writes_pg.py`,
  `tests/integration/test_artist_names_pg.py`,
  `tests/tasks/test_artist_identity_task_import.py`,
  `tests/tasks/test_backfill_artist_credits_task.py`,
  `tests/tasks/test_backfill_artist_credits_repeats.py`,
  `tests/services/test_linker_skips_shared_names.py`,
  `tests/services/test_pair_credit.py`,
  `tests/services/test_read_tags_all_artists.py`,
  `tests/services/test_read_tags_mp3_ufid.py`,
  `tests/services/test_enrichment_upserts_all_credits.py`,
  `tests/repositories/test_artists_mbid_conflict.py`,
  `tests/repositories/test_library_suggestions_contract.py`,
  `tests/repositories/test_artist_tag_files_contract.py`,
  `tests/repositories/test_artist_credit_files_contract.py`,
  `tests/repositories/test_artist_ids_by_mbid_contract.py`,
  `tests/repositories/test_artist_names_contract.py`,
  `tests/repositories/test_mb_cache_listing_contract.py`,
  `tests/repositories/test_artist_merges_contract.py` and
  `tests/tasks/test_enrichment_names_stubs.py`.

## The file-side version reader (C63, phase 2b S13)

- `normalization.extract_version_info` (its rules are `_VERSION_RULES`) reads a file's title:
  grouping takes the base title and the recording's version from it, enrichment a MusicBrainz
  recording's version (after MusicBrainz's own live flag), the untagged-file search its query
  title and version check, song matching's title scoring a dash suffix (`extract_dash_version`
  strips one only when it reads as a version), and `scripts/retitle_works.py` its base titles.
  It classifies only the first bracket tag that reads as a version (a group read as title
  words is skipped, so "(I'm Special) (Live)" reads live).
- Since C63 (D4) it reads album, LP and studio versions and "Original Mix" as the original,
  "Amended" as clean, and "(I'm Special)" as title words. Each rule sits just ahead of the one
  that misread it, so every reading above it still wins ("Live Album Version" is live).
- For grouping, a fix to it changes only new files and files re-scanned without a work: a
  grouped file keeps its work and recording, and no writer re-types a stored local recording.
  A stored MusicBrainz recording takes the new reading on its next enrichment upsert (when a
  pending file names it), which has no version-slot check; the regroup dry run counts the ones
  whose slot is taken. Title scoring and the untagged-file search read every title afresh, so
  they see a fix at once, on existing files too (an untagged "(I'm Special)" file no longer
  equals MusicBrainz's plain title).
- The log's abbreviations and venues (Lv, Unplgd, S&M2) stay off this reader: the log side gets
  its own (phase 2b S10, below), which never feeds grouping, matching or air play.

## Which file plays: version entry, format override, master (phase 2b, C59-C61, AUD-R033)

- `play_file_resolution` (migration 0030, replaced by 0050) picks one file per play. A matched
  line (auto or manual matched; its best match with a file, by confidence, then `created_at`,
  `id`) has the file's work, else that file's recording's work. The work's **version entry**
  for the line's version text plays first, else its **format override** for the station's
  format, else its **song master**; `source` is `version`, `override`, `master` or NULL, and
  is only logged. The version step is a second join of `format_overrides` by key: no
  subquery and no title parsing (AUD-R033). Six readers fence the view with
  `LATERAL … OFFSET 0`: the M3U repository, `playable_schedule.py`, `stream_cue_work.py`, the
  gate's `scripts/airplay_snapshot.py`, the guarded merge (`db/repositories/work_merges.py`)
  and the work page's lines (`db/repositories/work_page.py`).
- The full order since 3b PR 3 (0053): the stored performance (3a, "Credit picks" below), the
  version entry, the format override, the line's **merge pin**, the master. The pin is read
  from the same best-match lateral (`matches.pinned_file_id`); a pin on the master's own file
  reads as `master`, and a missing pinned file is reported, never fallen back from ("Work
  merges: the guard" below).
- A line's version text is `track_identities.version_text` (C59): a generated, stored, indexed
  column over `original_title`, its trailing round or square bracket groups lowercased,
  single-spaced and joined ("(Part 1) (Live)" is `part 1 live`), NULL without one. No writer
  sets it.
- A version entry (C60) is a `format_overrides` row with a `version_text` and a NULL
  `format_name` (a CHECK allows exactly one; `UNIQUE (work_id, version_text)` beside
  `UNIQUE (work_id, format_name)`). Every format reader (`list_by_work`, `get`, the work page's
  `format_overrides`) adds `format_name IS NOT NULL`, so a `FormatOverride` is never a
  version entry, and every upkeep path keys each kind on its own column (a NULL never equals,
  so no `IN`, `NOT IN` or `DISTINCT ON` over `format_name`): the merge's
  `drop_colliding_overrides` and the fold's `format_overrides.move_to_work`. `delete_for_file`
  deletes both kinds, and `delete_if_empty` keeps a work that has either.
- `version_entry_states` (work, version text, `dismissed` or none, `seen_at`) holds what a
  mapping can't. Its `work_id` cascades (D3), so a state never keeps a work alive. Migration
  0050 marked every (work, version text) on a matched line seen (S9). States follow the lines
  (D6): a works merge and the fold join copies of one song and combine (dismissed if either
  was, seen at the later time); split, reassign and `move_file_to_work` carry lines to another
  song, so the target gets each text as seen only, never a dismissal, and keeps its own. The
  old work keeps all of its own. The line copies are on `MatchRepository`, which every path
  that moves a file's lines already holds; the fold reads the folded row's lines before
  `merge_into` gives them to the survivor. The two conflict rules are written once, in
  `db/repositories/_version_entry_states.py`.
- **A gap kept on purpose, both kinds (D1):** split and reassign (when the old work survives)
  and `move_file_to_work` (enrichment, Set master) move a file without its overrides, so a
  format override or a version entry can name a file now on another work, and the view still
  plays it. Moving or deleting them would change what those lines play.
- The log-side reader, `domain/log_version_reader.read_log_version` (C63, S10), reads a version
  text as a `VersionType` (UNKNOWN for title words): the file side's table as phase 2b PR 1
  left it, plus the log's abbreviations and venues (Lv, Lv. Ac., Unplgd, Acous, Stripped, S&M2,
  BBC, Telethon, Ed Sullivan). Only the Library's version pages read it (phase 2b PR 3); it
  never feeds grouping, matching or air play
  (`tests/domain/test_log_version_reader.py::test_grouping_matching_and_air_play_never_read_it`).
- The checks are `tests/integration/test_version_text_pg.py`,
  `tests/integration/test_version_entries_schema_pg.py`,
  `tests/integration/test_version_entry_upkeep_pg.py` and
  `tests/integration/test_view_version_order_pg.py`, beside the locked
  `tests/integration/test_play_file_resolution.py`.

## Version pages: Map, Dismiss, the new-version marker (phase 2b PR 3, C62, C64, C66)

- A work's version texts are those on its matched lines (the view's rule above: the best
  match's file's own work, else its recording's) plus the texts of its version entries, so an
  entry left behind by D1 is listed as mapped with 0 plays. `db/repositories/version_entries.py`
  reads them with their plays, stations, first play and state in one query; the pure rules are
  in `domain/version_pages.py` and the writes in `services/version_entry_service.py`.
- Each text is read by the log-side reader. Title words and originals (UNKNOWN, ORIGINAL) are
  not versions: they are listed under "Title words" on the work page, can be mapped, are never
  new and never on the Unmapped versions page.
- State is mapped (an entry), else dismissed (a `version_entry_states` row), else unmapped.
  **New** (S8, D2) is an unmapped version with no state row, or with a copy (a present file of
  the work whose recording's `version_type` is the reading) indexed after the row's `seen_at`.
  Map writes the entry and marks the text seen now; Unmap deletes the entry and keeps the state;
  Dismiss refuses a mapped text (409) and marks it dismissed and seen now.
- Suggestions (S11) are read, never written (C64, AUD-R046): the one copy the text's lines are
  matched to, else the work's one copy. Only Map, on Lance's press, writes an entry. The master
  flag (S12, C66) is a flag too: the master's title reads live, acoustic, demo, remix or
  extended and the work holds a plain copy.
- Counts: the Unmapped versions page (`/library/versions`, by plays, paged at 50) answers its
  total and new total for the Library Status card; the artist list's `new_version_count` sums
  the group's works on the group primary (D5). Dismiss all on that page takes every listed
  text; on an artist page, only the group's new ones (question D). The artist list's and the
  artist page's counts (`new_counts`) read the listed works' texts, then the files of only the
  works that have one, and no line files: a count shows no suggestion.
- No summary table of its own: plays and stations come from `station_identity_plays` (below),
  so they count only plays in a station's playlists, and each line's first play is looked up
  through that line's own plays. Every query has an every-work and a scoped variant; the scoped
  one reads only identities with a match on the works' files. On the 2026-11-09 gate copy (7.55M
  plays) the page's query took 156 ms and the page 240 ms, down from 1.26 s and 1.29 s reading
  `play_events` (Lance: "Rewrite + rerun"). If it outgrows a second, the SPEC's fallback is an
  ingest-kept summary (a migration).
- The checks are `tests/routers/test_work_versions.py` (AUD-R046) and
  `tests/routers/test_unmapped_versions.py` (AUD-R034), and for the artist pages' counts
  `tests/routers/test_artist_new_version_counts.py` and
  `tests/services/test_version_new_counts_reads.py`.

## Song matches: one writer (phase 3a, C52, C75, AUD-R074)

- `SongMatchService` (`services/song_match_service.py`) is the only code that calls the matches
  port's writes (D2): the auto-matcher's result (`record_result`), Resolve's manual match
  (`match_by_hand`), Reject's and Unmatch's clearing (`clear_song`), a deleted file's release
  (`release_file`), and a works merge's re-point of the lines targeting a merged work
  (`follow_works`). Since 3b PR 5b a match carries no work of its own (a line's work is its
  file's, AUD-R083), so a file's move carries nothing along. It is sync over the port and commits
  nothing: it writes in the caller's transaction. Bundles that locked tests build by keyword
  keep their port field (`IdentityMatchingRepos(match_repo=...)`, `matches=` elsewhere), and
  their services wrap it in the service.
- The port's adapter (`db/repositories/matches.py`) holds the SQL; the works merge's and a left
  work's match statements moved there from `WorkCurationRepository`. Five repository statements
  stay where they are, with their locked tests and fakes: the two cascade CTEs of
  `broadcast_track_identities` (`bulk_reject_by_artist`, `bulk_defer_by_artist`),
  `artist_cascade.delete_matches_of_lines`, `artist_resolution.delete_pending_song_matches` and
  the fold's re-points in `library_files.merge_into` (the matched file and, since 3b PR 3, the
  pin). Phase 2b's version-state copies sit on the
  port but write `version_entry_states`, so they stay outside the service.
  `scripts/probe_truncate_cost.py` truncates a test database's tables and is tooling, not a
  writer.
- A song's Resolve, Reject and Unmatch are sync routes on `SyncRepos` (C75), calling
  `identity_resolution_service` with `SongDecisionRepos`: the song's row through
  `SongDecisionRepository` (`repositories/song_decisions.py`; Reject and Unmatch take its
  `FOR UPDATE` lock first, Resolve its row's update), its matches through the service, so status
  and matches commit together. `record_song_rejection` and `persist_manual_match` keep their
  names (AUD-R029 stands). Resolve commits, then recalculates the work's master on its own
  connection (`recalculate_for_work_sync`, best effort, as before). A lost connection answers
  503.
- The checks are `tests/test_song_match_single_writer.py` (a scan of `backend/` and `scripts/`:
  only the adapter and the five statements hold SQL that writes `matches`, and only the service
  calls the port's writes) and `tests/routers/test_identity_routes_are_sync.py`.

## Performances, kept by every writer (phase 3a PR 2, C40, C44, C71, AUD-R075; 3b PR 2, AUD-R078)

- **The model (0051).** `performances` holds one recording of a work: its work, an optional
  recording MBID (unique within the work), the file-side reader's version, its first file's
  length, and a preferred copy. `performance_credits` holds its credit (position, artist or
  MBID, credited name, joining words, main or featured). An MBID no artist holds yet is
  credited by MBID, and readers find the artist on the MBID later (D1: no stub). Every
  performance credits someone (3b D1): one whose first file has no credit left after
  placeholders credits its work's artist (since 3b PR 5b: the artist its writer passes, else
  the work's default act; see "A work's artists" below) by id, with its MBID if it has one; a
  placeholder work artist gives none. A file points
  at its performance through `(performance_id, work_id)`, so a file and its performance always
  share a work; the key is checked per statement, so every `work_id` write clears
  `performance_id` in the same UPDATE. A performance's preferred copy is one of its own files
  (a composite key on `library_files (id, performance_id)`). `matches.performance_id` is a
  line's stored performance; nothing stores one before PR 3's Credit picks. `song_masters`
  stays each work's default copy (C71).
- **The identity rule (§6.2, `domain/performances.py`).** A file joins a performance of its
  work with the same recording MBID; a file with an MBID joins nothing else. A file without one
  joins any performance of its work (MBID or not) with the same credit artists, version and a
  length within 3 s of the performance's `length_ms`; the closest wins, then the one whose
  first file has the lower id. A NULL or negative length never joins. Credit artists are sets,
  placeholders left out, each credit as its MBID when known (its own, else its artist's as it
  is now), else its artist id (3b D1, AUD-R078): two name-only acts stay apart, and an artist
  promoted in place keeps every key. A file with no credit of its own counts as the performance
  it holds or leaves (3b D18), else as its work's artist (since 3b PR 5b: grouping's pick, the
  seed for the build, else the work's default act), so an untagged copy joins its artist's
  tagged performance when version and length agree.
- **The build (`tasks/performance_build_tasks.py`).** A run-once task, after 2a's credit
  backfill marker: every file on a work, present or missing, in file-id order, in committed
  batches, so a stopped run resumes. Its own marker (`library.performances_built`) ends it;
  from then on the writers keep performances. It reports as `performances_build`. Nothing
  queues it: the cutover and the gate run it by name. Since 3b PR 5b a file's work artist is
  its work's row in `work_artist_seed` (D38), passed to `assign_performance`.
- **D1's step (`tasks/work_artist_credit_tasks.py`, 3b PR 2).** A run-once task after the
  build's marker gives every performance with no credit its work's artist (none for a
  placeholder; since 3b PR 5b, the work's row in `work_artist_seed`), in committed batches,
  with no MusicBrainz call and no file moved. Its marker is `library.work_artists_credited`; it
  reports as `work_artist_credits`. The build already credits what it makes, so on the live
  data it credits almost nothing (3b D20); it covers performances made before 3b. The merge
  planner (3b PR 4) waits for its marker.
- **D1's step changes the schema (3b PR 5b, D49).** It is the one run-once step that does: in
  the transaction that sets its marker it drops `work_artist_seed` (`drop_seed`,
  `DROP TABLE IF EXISTS`), so a re-run, which finds the marker, is a no-op. The rollback is
  `backend/db/rollback_0055_work_artists_from_credits.sql` (see "A work's artists" below).
- **The writers (S3, `services/performance_service.py`)** keep one order: `leave` (re-pick a
  preferred copy that is leaving), the writer's own move, `assign_performance` for each file
  that now sits on a work (a moving file with no credit of its own keeps the act of the
  performance it left, 3b D18), then `settle`. Grouping (the scan and the watcher), the work move
  (enrichment, Set master), merge, split, reassign, the fold, the missing-file delete and the
  purge all do. The bundles that locked tests build by keyword (`WorkMoveRepos`,
  `EnrichmentRepos`, `ReconciliationRepos`) carry the port as an optional `performances`
  field, as does `WorkCurationRepos`; every builder in `backend/` passes it.
- **Settling (E3, D5).** A line storing a performance the writer touched is handed off when
  the performance emptied and all its files now sit in one performance of the line's own work
  (a folded file sits where its survivor does); it keeps its performance when that is still in
  its work with files; otherwise it is released to the default. Both are logged
  (`stored_performance_handed_off`, `stored_performance_released`) and go through
  `SongMatchService`. An emptied performance is deleted, before any work the writer deletes,
  so the cascade never drops a stored line unlogged.
- **D6, tags only.** The scan and the watcher re-check the files they re-read: a file whose
  recording MBID tag, title version, length or credit no longer fits its performance is placed
  again in its work (one read per 1,000 paths). A file with no credit of its own is compared
  with the credit artists of the performance it holds, so only a tag change moves it (3b D18). Enrichment's recording link and the curation's
  recording re-points never move a performance.
- **Preferred copies (S5, E8).** A path that takes a preferred copy out of its performance
  re-picks it inside the performance: the best copy left by the master rules, present before
  missing (a fold onto a copy of the same performance re-points it to the survivor, as it does
  a master). A performance given a line gets its best present copy if it had none. After a
  scan, `repick_stranded_masters` also re-picks preferred copies gone missing among present
  copies; with none present the copy stays. Set master makes the chosen file its performance's
  preferred copy when that performance has one.
- The checks are `tests/integration/test_performances_schema_pg.py` (AUD-R032),
  `tests/tasks/test_build_performances_task.py`,
  `tests/integration/test_writers_keep_performances_pg.py`,
  `tests/integration/test_preferred_copy_upkeep_pg.py`, and for 3b's D1
  `tests/domain/test_performance_key.py`, `tests/integration/test_default_credit_pg.py` and
  `tests/tasks/test_credit_work_artists_task.py` (AUD-R078).

## Credit picks: the credit picks a performance (phase 3a PR 3, C47, C57, C61, C64, C65, AUD-R076; AUD-R078)

- **`choose_performance` (`domain/performances.py`, C47, D3, D8, D9).** Pure: the log name's
  active links against the work's performances' credits, every artist as its merge-group
  primary. A performance answers better when it misses fewer of the line's artists, then
  credits fewer others: missing none is `credit`, missing some but sharing one is
  `shared_artist` (M21), sharing none is `no_performer`. The default (the performance of the
  master's file) wins any tie with the best; tied non-defaults go to Lance. An untagged
  default counts as its work's artist (D8) through the credit 3b's D1 gives it, so the function
  reads credits alone; a performance with no credit at all shares no artist. A name with no
  linked artist gets the default (C16). 4.3's matcher is to use it as it is.
- **Air play (0052, C61, C71, AUD-R033).** `play_file_resolution` plays the performance the
  line's best match stores (its preferred copy, `source = 'performance'`), then the version
  entry, the format override and the master. The stored performance comes from the existing
  best-match lateral and is key-joined on (id, the line's work), so another work's performance
  never plays; it is skipped while it is the default, so Set master still moves the line. An
  unavailable file is reported, never fallen back from. The view still only looks up keys.
- **Proposals (`tasks/credit_pick_tasks.py`, S8, C57 as D4 amends it).** After the build's
  marker, a re-runnable task (`credit_pick_proposals`) writes one `credit_pick_proposals` row
  per matched line whose credit answers with a non-default performance: `credit` (Apply's
  list), `version_line` (a version `read_log_version` recognises) or `tie` (both for a per-line
  pick, C65). A re-run keeps decided rows, keeps an undecided row only while its answer is
  unchanged, and gives a changed answer a new row (C64). It changes nothing that plays.
- **Deciding (`services/credit_pick_service.py`, S5, S9, M28).** Apply stores the `credit` rows
  Lance saw minus his skips and records the skips; a version-line pick (one candidate, or none)
  is stored at once; `scripts/apply_credit_picks.py` does Apply on a gate copy. All three store
  through `SongMatchService` on the line's best match, giving the performance its best copy as
  preferred copy first. A row stale when read or applied (its line re-resolved, its
  performance gone, on another work or now the default) is left out of the lists and the count
  and never applied. The page is reached from Library Status (D7).
- **Rollbacks.** `rollback_0052_credit_picks.sql` puts back 0050's view and drops the table;
  `rollback_0051_performances.sql` undoes 0052 first (idempotently), since the view and the
  proposals depend on what 0051 adds.
- The checks are `tests/domain/test_choose_performance.py`,
  `tests/integration/test_view_stored_performance_pg.py` (AUD-R033),
  `tests/integration/test_stored_performance_pg.py`,
  `tests/tasks/test_credit_repoint_proposals.py` (AUD-R039), `tests/routers/test_credit_picks.py`
  and `tests/scripts/test_apply_credit_picks.py`.

## Work merges: the guard, pins, the log (phase 3b PR 3, C58, C72, C74, AUD-R079)

- **The guard (`services/work_merge_service.py`, S3, D3).** `guarded_merge(group, repos)` is
  the only way two works merge. A `WorkMergeGroup` is a target, its sources, a reason
  (`library`, `merge list`, `musicbrainz work link`) and Lance's approvals. In a savepoint on
  the caller's connection it reads what each (station, line) of the group's works plays through
  the view (one play per station stands for all, from `station_identity_plays`), runs
  `merge_works`, reads again, and keeps the merge only when every change is approved. An
  approval is (station, line, new file) (Q8), so it covers that one change only. Otherwise the
  savepoint is undone and `MergeRefused(changes)` is answered. The pure rules (`changes_between`,
  `all_approved`) are in `domain/work_merges.py`; the reads, the savepoint and the log are on
  `WorkMergeRepository` (`repositories/work_merges.py`, adapter
  `db/repositories/work_merges.py`).
- **What `merge_works` holds (S3, S4, D2, D25).** Before anything moves it reads the lines of
  the group's works that play a master somewhere (`master_lines`). After the master is
  re-picked, each line whose master's file is no longer the master keeps it:
  - a line whose log name's links pick a **non-default** performance of the merged work that
    holds the file stores that performance, with the file as its preferred copy (S4: a cover
    act keeps its own copy);
  - any other line is **pinned** to the file (`SongMatchService.pin`, `matches.pinned_file_id`).

  A stored line is never pinned: its change is a flag (D2). A line no station plays through its
  master (an override or a version entry plays instead) is not pinned (D25), so if that entry
  goes later, the line plays the merged work's master. Changes a pin can't hold (a stored line,
  an override or version entry crossing acts) are what the guard asks about.
- **The routes (D3, Q7, Q19, D22).** `POST /works/{id}/merge` answers 409 with
  `{"message", "changes": [...]}` (each change's station, line, plays, and from and to file)
  when a change waits, and `POST /works/{id}/merge/approved` takes the same body plus
  `approvals`. A different change found on the second call is a 409 again. The artist page's
  "Merge N works" shows the list ("What this merge changes") with "Merge anyway" and "Cancel";
  the client's `ConflictError` keeps an object detail. A failed merge answers 500; the
  request's rollback takes its `failed` log row with it, and the `work_merge_failed` log line
  stays.
- **Pins kept right (`services/pin_upkeep.py`, D4, D14, D23, E7).** `pinned_file_id` has no
  `ON DELETE` action, so a path that forgets fails at the foreign key. A pin stands in for its
  work's master only, so every path that deletes a file (Missing files' delete and the purge,
  both through `_delete_missing_row`) or moves it to another work (split, reassign,
  `move_file_to_work`, a fold onto another work) releases its pins first, logged as
  `pins_released`. A fold onto a copy on the same work re-points them in `merge_into`. Lance
  releases a line's pins by hand on the work page (3b PR 5a, "The work page" below).
- **The log (0054, S10, Q18).** `work_merges` has one row per merge: merged, failed (with its
  error) or rolled back. It has no foreign keys, because the works it names may be gone and
  0051's rollback drops performances. `work_redirects` maps each merged-away id to the live work.
  A later merge of that work re-points its redirects, so a chain resolves in one step. The work
  detail answers a redirected id with the live work, and the work page replaces its URL (Q19).
  `work_merge_rows` is the rollback map: each row the merge changed in thirteen tables (3b PR 5c's
  0056 adds the keep-apart marks), keyed by table and key, with the row as it was, or NULL for a
  row it inserted. A capture trigger
  (`work_merge_capture`) fills it, so cascades and `SET NULL`s are caught with no code in the
  merge. The trigger acts only while the transaction-local setting `retrostation.merge_id` is
  set, between the guard's `start` and `stop`. A failed merge is undone alone (its savepoint),
  logged after the rollback, and raised as `WorkMergeFailedError`.
- **The restore and rollbacks.** `backend/db/rollback_work_merges.sql` (by hand, app stopped,
  `psql -1 -f`) restores every merged merge, newest first:
  - parents come back before children, and rows the merge inserted go;
  - preferred copies are cleared first and put back last;
  - the merge's redirects go, and it is marked `rolled_back`.

  `rollback_0054_work_merge_log.sql` drops the log and every capture trigger (0056's on the
  marks too), and `rollback_0053_merge_pins.sql` puts back 0052's view and drops the pins. The
  restore skips a table a later rollback dropped. Run the restore first: dropping the pins loses what they
  held.
- The checks are `tests/integration/test_work_merge_guard_pg.py`,
  `tests/integration/test_view_pin_order_pg.py` (AUD-R033),
  `tests/integration/test_work_merge_rollback_pg.py`, `tests/routers/test_work_merge_approval.py`,
  `tests/routers/test_work_redirects.py`, `ArtistDetail.mergeConfirm.test.tsx` and
  `AssociatedWorks.redirect.test.tsx`; their seed is `tests/integration/work_merge_seed.py`.

## The merge list, one deletion rule, the recording index (phase 3b PR 4, C53, C57, AUD-R080)

- **The planner (S1, D29).** `domain/work_merge_planner.py` is pure; `merge_plan_service`
  feeds it each work's facts from `MergeListRepository.plan_works`:
  - the title key: grouping's exact base-title rule (`title_key`), never its fuzzy match;
  - the work MBID, and the work MBIDs its performances' cached recording lookups name;
  - its credits' artists (merge-group primaries, else MBIDs) and names, placeholders out.

  Works join on a shared work MBID, or on an equal title key with a shared artist or credited
  name; a title alone never joins. The title joins of one key stand only while the works they
  join hold at most one work MBID (Q15). The target is a MusicBrainz work, then more matched
  plays, then the lower id. Until D1's marker the planner refuses (`CreditsNotReadyError`, Q22).
- **The page (S2, D6, D15, D27, D28, D37).** `GET /library/merge-list?offset=&limit=` plans on
  every read (it stores nothing, Q13) and answers one page of the groups, most played first:
  50 a page (`PAGE_SIZE`, the most `limit` takes), with `total` counting every group. It tries
  each of the page's groups' guarded merge with `work_merge_service.preview`, in a savepoint
  always undone, with no log row and no capture, and shows the changes a pin can't hold. Beside
  them are flags that only show: hand-picked masters meeting, a cue edit on a master's audio, a
  widened rejection. It also shows the group's last failed attempt; the recording MBIDs still
  under two works, the empty works kept and the kept pairs (PR 5c) come whole with every page.
  The count route uses the planner alone; Library Status shows the planned-group count while
  any of the four sections has rows (D28, D58).
- **Pages (D37).** One bar above the groups: Apply, Previous, Next, "Groups 51–100 of N". Ticks
  and skips belong to the page shown and clear on a page change. After an Apply the same offset
  loads again, so the groups after the merged ones move up; a page an Apply emptied steps back
  to the last page with groups.
- **Apply (D26, E5).** `merge_list_service.apply` plans again and merges each group of the page
  Lance saw, as he saw it, with his approvals, through `guarded_merge` (reason `merge list`). It
  commits after each group (the route's commit port), so a failed group's log row is kept on
  its own.
  Each group answers `merged`, `waiting`, `failed`, `already_merged` (every source redirects to
  the target) or `changed` (its works no longer plan together; nothing is merged).
- **Keep apart (PR 5c, D40, D55-D58, AUD-R089).** `work_keep_apart` (0056) holds each pair of
  works Lance keeps apart, the lower id first under `COLLATE "C"` (the planner's `WorkPair`,
  plain string order), with when and the writer's note; either work's delete takes its marks.
  - The planner never joins a kept pair (its link edges are every pair sharing a work MBID,
    minus kept pairs; kept title joins drop before Q15's count). A set other works still join
    is *held*: its group shows a notice and its pairs, has no Skip tick, isn't previewed, and
    Apply's plan leaves it out (a send of it answers `changed`).
  - Writers: the page's "Keep apart" (`POST /library/merge-list/keep-apart`) marks every pair
    of a group's works (D55), noted "Kept apart on the merge list"; "Kept apart", the page's
    last section, lists every pair with Undo (`POST .../kept-apart/undo`, 204 even for a pair
    not kept). Split and reassign mark their own pair while the work left exists ("Split off
    <file>", "Reassigned <file> to <title>"); nothing else that moves a file marks.
  - A merge carries its sources' marks to the merged work (`WorkKeepApartRepository.carry`:
    a self-pair drops, of two landing on one pair the earlier stays), captured for S10's
    restore. The guard refuses a group holding a kept pair for `musicbrainz work link` (E4) and
    `merge list`, logged `work_merge_kept_apart`; the Library's merge goes through (D57). A row
    whose works a work MBID joins says "MusicBrainz links these works".
- **One deletion rule (C53, D7, D30).** `WorkRepository.delete_if_empty` and
  `kept_empty_works` read one map of references:
  - a recording, a match on the work (`matches.work_id`), a match whose target is the work;
  - a format override, a version entry;
  - a performance of the work that a match stores.

  A pinned file is a file, so the files clause covers it. A keep-apart mark is no reference
  (it cascades with the work). `sweep_empty_works_task` (no marker,
  run by name after the merges and after phase 4's) deletes every file-less work nothing points
  at.
- **The global index (S5, C40, E6).** `build_recording_index_task` builds
  `uq_performances_recording_mbid` with `CREATE UNIQUE INDEX CONCURRENTLY`, on an autocommit
  connection, never in a migration. It runs only once `PerformanceRepository.duplicate_recordings`
  is empty, and otherwise names the MBIDs. An invalid index left by a failed build is dropped
  and built again.
- **Order at the cutover.** 3a's build, D1's step, the merge list's Apply, then the Credit
  picks proposals task (nothing queues it, D33), the sweep, and the index.
  `scripts/apply_merge_list.py` is the gate's Apply: it writes the work map and an allowlist
  of what its merges change (S11, D32), with each merged-away work's golden detail, which now
  answers its target's (D35).
- The checks are `tests/domain/test_work_merge_planner.py`, `tests/routers/test_merge_list.py`,
  `tests/tasks/test_empty_work_sweep.py`, `tests/integration/test_global_recording_index_pg.py`,
  `tests/scripts/test_apply_merge_list.py`, `MergeList.test.tsx` and
  `LibraryStatus.mergeListLink.test.tsx`. PR 5c's are
  `tests/domain/test_work_keep_apart_planner.py`, `tests/integration/test_work_keep_apart_pg.py`,
  `tests/routers/test_merge_list_keep_apart.py`, `MergeList.keepApart.test.tsx` and
  `LibraryStatus.keptApartLink.test.tsx`.

## The work page: performances, copies, pins (phase 3b PR 5a, S8, D41, AUD-R081)

- **Reads.** `GET /library/works/{id}` and `/versions` call `work_page_service`, which reads
  through `WorkPageRepos` on the request's async connection (the `WorkPageReader` port, adapter
  `AsyncWorkPageReader` in `db/repositories/work_page.py`, and the version entry reader) and
  hands the facts to `domain/work_page.py`. The domain is
  pure: the performances' order (the default first, then credit, the original before other
  versions), each credit's text (its joining words, else ", "), the lines pinned on each copy
  and the file each would play next once released (the master after a pin step, else the file
  it plays now), and the acts.
- **The detail (D41 a, b, g, h; D45, D46).** It keeps `recordings` and adds `artists` (the
  work's credited artists, for the header), `performances` (each with its credit, version,
  `default` flag, preferred copy, how many lines store it, and its copies with their pinned
  lines), `acts` and `other_work.artists`. The default is the performance holding the master's
  file (C71). The page lives at `/library/works/:work_id` as well as under its artist. Copies
  with no performance yet and copies on other works (a shared recording) are two lists below
  the performances; the copy table they replace is gone.
- **The lines.** The page reads each (station, line) whose best match's file is on the work
  through the air-play view, one play per station, fenced like every reader ("Which file
  plays" above). `source` tells a pinned line's pin step from a master that is its own file.
- **Prefer this copy (D41 c).** `PUT /works/{id}/performances/{pid}/preferred-copy` sets a
  stored performance's preferred copy (`master_choice_service.prefer_copy`). It refuses the
  default performance, whose copy Set master picks, a file not a copy of the performance and a
  missing copy.
- **Release (D41 d, e).** `POST /works/{id}/pins/release` clears the named lines' pins on
  copies of the work (`work_curation_service.release_line_pins`, through
  `SongMatchService.unpin`), logged as `pins_released`; a line that pins no copy of the work
  refuses the whole request. Before Set master replaces a copy holding pins, the page lists
  those lines unticked and releases the ticked ones first (C72).
- **Two acts (D41 f, C65).** With two or more acts, Add override and Map versions show how
  many of the default act's lines the override or entry reaches at each format, and how many
  lines of each other act keep their own (those whose stored performance plays).
- The checks are `tests/routers/test_library_works_services.py`,
  `tests/domain/test_work_page.py` and the seven `AssociatedWorks.<aspect>.test.tsx` files
  (`performances`, `copyRow`, `preferredCopy`, `pins`, `setMasterPins`, `twoActs`, `address`).

## A work's artists are its performances' credits (phase 3b PR 5b, S7, D19, D38, D47, D49, D50, AUD-R082-R088)

- **The schema (0055).** `works.artist_id` and `matches.work_id` are gone, with their indexes
  (AUD-R082, AUD-R083). A work's artists are the union of its performances' credits, a credit
  by MBID alone naming the artist that holds the MBID; a work with no performance (no file) has
  none (D42). A line's work is its file's work.
- **Readers.** The artist list counts, and an artist's page lists, the works its performances
  credit, a merged member's on its primary's (R6). These works reads left
  `routers/library/artists.py` for an async reader (R9): the port `ArtistWorksReader`
  (`repositories/artist_works.py`), the adapter `AsyncArtistWorksReader`
  (`db/repositories/artist_works.py`, whose `_WORK_ARTISTS` is the rule), wired by
  `get_artist_works_reader`. The list pages its artists first, then reads only those groups'
  credits, by artist id and by held MBID, both indexed (SPEC D53: building every credit pair
  first took 325 ms a page on the dev copy). An Unmapped versions item names its work's
  credited `artists`, and an artist page's Dismiss all takes the works its group is credited
  on (R7). The work
  page's header and "On other works" name the credited artists, "No credited artist" when
  there are none and the address names no artist (D47, D50; `credits.ts`). Enrichment's MBID
  claim needs the local work's credits to hold the artist (R3); the untagged pass searches with
  the MBID of the file's own performance credit (R4).
- **Grouping (R1).** A file's candidates are the works whose performances credit an artist of
  its normalized name, and (D51) the works whose files of that name hold a credit of an MBID
  no artist holds: a name with one MBID makes no artist for it (2a D6), and without this
  tagged copies of one song would each make a work (`_UNHELD_OF_NAME` in
  `db/repositories/works.py`). The two sets are one `IN (... UNION ...)` so their indexes
  drive the read (SPEC D53: an `OR` of two `IN`s scanned every work, 88 ms a name on the dev
  copy). #258's tie order stays. The artist grouping picks is the one of
  the name the chosen work credits, else `upsert_local_artist`'s for a new work;
  `GroupingResult.artist` carries it to `assign_performance(default=)`.
- **A file with no credit of its own** counts as the performance it left (D18), else the
  artist its writer passes (grouping's pick; the seed for the build), else (a reading) its
  work's default act, the credits of the performance holding the master's file; else no one.
- **The seed (D38, D49).** 0055 copies each work's artist into `work_artist_seed` (work id,
  artist id; both cascade) before it drops the column. Only the build (`files_waiting`) and
  D1's step (`uncredited`) read it, and D1's step drops it when it sets its marker (above).
  The rollback restores `works.artist_id` from the seed while it exists, else from each work's
  default performance's first credit (NULL when neither), `matches.work_id` from each match's
  file, and both indexes, then drops the seed.
- **Merges (D19).** A merge deletes the local artists credited on its source works that nothing
  names any more after it (`artists_credited_on`, `_delete_unused_local_artists`).
- The checks are `tests/integration/test_work_artists_from_credits_pg.py`,
  `tests/integration/test_work_artist_seed_pg.py`,
  `tests/integration/test_merge_deletes_uncredited_artists_pg.py`,
  `tests/audit/test_rulings_phase_3b_pr5b.py`, `AssociatedWorks.uncredited.test.tsx` and
  `UnmappedVersions.credits.test.tsx` (AUD-R088).

## Derived counts kept by triggers

- `station_identity_plays` (migration 0040) is COUNT(*) of `play_events` per (playlist's
  station, identity). Triggers on `play_events` (insert / delete / update / truncate) and on
  `playlists` (delete, station change) keep it equal, so no service writes it and no task
  reconciles it. The station missing-matches report and the version pages (above) read it
  instead of scanning every play.
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

## Song endings: the fade after EOM, by the shape of the ending

- `stream_endings` (migration 0049) holds one measured ending per audio, keyed by `audio_hash`
  like `stream_cues`: `cold`, `long_tail`, `other` or `unmeasured`, the autocue Stop it was
  measured against (`measured_cue_out_ms`), and a long tail's measured EOM (recorded for stage 2,
  never played). Its own `ENDING_ANALYSER_VERSION`, so an ending change never re-runs autocue.
- The run is `services/streaming/ending_analysis.py`, a periodic task on the cue worker
  (`stream_ending_analysis_task`, every 5 minutes): purge other versions, prune endings whose
  audio lost its cues, then measure (`playout/loudness.py`, FFmpeg) and classify
  (`domain/endings.py`) batch by batch. An unreadable file is stored `unmeasured`; a missing
  FFmpeg, or a batch of 4+ files all unreadable, ends the run.
- Precedence, on both radio cue reads (`db/repositories/playable_schedule.py`) and in the cue
  editor's opening view: a manual edit, then the ending rule over autocue's row, then autocue's
  row. The rule applies only while autocue's row keeps the Stop it was measured against.
- The rule (Lance, 2026-11-07, "Fades only, EOM as-is"): EOM stays autocue's for every song. A
  cold ending plays to autocue's Stop with no fade; otherwise Stop = EOM + the fade after EOM
  (2 s after a long tail, 1 s after anything else), cut short only by autocue's Stop. When a
  liner is due right after a long tail, the stream service shortens its fade to 1 s
  (`StreamService._before_liner_if_due`); the day read carries the ending on
  `PlayableFile.ending` for this, and only when the rule was applied.
- Rollback: `DELETE FROM stream_endings` puts every song back on autocue's points.
- Its checks are the domain, loudness, run, liner-fade, cue-editor and Pg integration tests
  named `*ending*`, `*long_tail*` and `test_loudness.py`, and the acceptance test over Lance's 51
  labelled picks (`tests/fixtures/endings/lance_picks.json`).

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
  a scope word, or nothing). Event graph 2026-10-09: 17 commands, 0 events, 0 queries, no cycles;
  2026-11-22 (phase 4.1 PR 2): 20 commands, one cycle (the cache fill's batch chain).
  One exception (AUD-R030): a matching stage of a re-check's run also carries the run's id
  (`artist_matching_task(playlist_id, run_id)` → `identity_matching_task(playlist_id, run_id)`),
  so it can report to the run's progress row. The id is optional: an upload's matching has none.
  A second (phase 4.1, AUD-R091): a cache-fill batch carries its run's state, one JSON string
  (`FillRun`), and hands the next batch to itself, the graph's one cycle (EV04) and a bounded one:
  each batch makes at least one call, and every call it makes is cached or set aside for the run.
- **The work list lives in status columns**, not in the message. These columns are the contract
  between tasks (AUD-R015):

| column / predicate | written by | read by |
|---|---|---|
| `broadcast_artists` / `track_identities.match_status` (+ `reason_code` DEFERRED_RETRY, MB_UNAVAILABLE, LINK_REJECTED, LINK_SUGGESTED, LINK_REMOVED) | ingest, matchers (an artist's status from its links through `LogNameLinkService`, C36: a match held back on a rejected link is `needs_review` / LINK_REJECTED, phase 1b S4; an answer under 95, or an exact name of several artists, is LINK_SUGGESTED, phase 1c S4), review UI, re-check rewind, migrations 0036 (once, AUD-R025) and 0045 (once, AUD-R039) | artist / identity matching; the re-check rewinds every MB_UNAVAILABLE song (AUD-R028, extended by AUD-R042) |
| `track_identities.rejected_file_ids` | Reject / Unmatch (API), library_files.merge_into | song matching skips those files and their current works |
| `library_files.indexed_at` / `missing_since` after the newest COMPLETED `matching_recheck` run's `started_at` (`progress_tracking`) | scan / watcher upsert and relocate (`indexed_at` moves only for a new, back-from-missing or size/mtime-changed file, AUD-R023 D11), `mark_missing` | targeted re-check (`rematch_undecided_task`) |
| `library_files.audio_hash IS NULL` | scan / watcher upsert | hash backfill, cue analysis |
| `library_files.enrichment_status` | scan / watcher, enrichment, retry button | library enrichment (pass 4 takes the `pending` files with no MBID tags whose own performance credit has one (3b PR 5b, R4), `list_untagged_with_linked_artist`; a file it cannot decide stays `pending`) |
| `artists` / `works` / `recordings.needs_enhancement` | MusicBrainz upsert; stub artists (migration 0044, the artist matcher and Resolve, phase 1a); for `works`, also enrichment's MBID claim on a local work (AUD-R103) | MB enrichment |
| `artists` with `origin = 'local'`, no `mbid`, and `mb_lookup_at` NULL or older than a present file's `indexed_at` | grouping (`upsert_local_artist`), the linker's stamp (`mb_lookup_at` / `mb_lookup_outcome`, migration 0037, AUD-R026, in part superseded by AUD-R036) | local-artist linking (`link_local_artists_task`) |
| `artists.mb_lookup_outcome = 'linked'` with `mb_lookup_at` after the newest COMPLETED `matching_recheck` run's `started_at` | the linker | targeted re-check: the second source of its wave's names, the artist's own and its active log-name links' (AUD-R026, in part superseded by AUD-R036) |
| `library_files.artist_mbid` of a name holding two or more real MBIDs, one with no artist or no `shared_name` row; `library_files.credit` of a present file holding two or more real MBIDs, one with no artist (phase 2a S14) | scan / watcher upsert; the artist credits backfill | the artist identity sweep, last in a scan, in the scan's own task (no hand-off), and once from the backfill |
| `library_files.artist_mbids IS NULL`, or a `credit` holding a null name, on a row with `raw_metadata`, until `library.artist_credits_backfilled` is set (phase 2a S3) | scan / watcher upsert (only for a new or size/mtime-changed file, or unfilled columns); the artist credits backfill | the artist credits backfill |
| no `stream_cues` row for the file's hash (`stream_cue_work.NEEDS_ANALYSIS`), or for a station liner's hash (LR4) | cue upserts | cue analysis (liners first) |
| `stream_cues.audio_silent` (D111) | cue analysis (a silent verdict); migration `0039_stream_cues_audio_silent` deleted the failed rows and the floor rows once | schedule resolution (the song gets no file, so tune-in and the walk skip it); both M3U exports; the Library Silent Files report |
| `library_folder_staged_hashes` | watcher poll | watcher scan |
| the matching run's `progress_tracking` row (`matching`, `stages_left` / `stages_failed`, AUD-R030) | the re-check opens it; each matching stage refreshes it and finishes its stage; the last stage closes it | the matching stages, `/ws` |
| `progress_tracking` | every task envelope | `/ws` poll (one shared `ProgressFeed`, 0.5 s, only while a client listens), hash-backfill liveness; the daily prune drops finished rows after 14 days, except each type's newest completed row (the re-check watermark); the cache fill's guard (a RUNNING `mb_cache_fill` row) |
| no live `mb_cache` row for a fill query's key (`recording-name-title:`, C18's `recording-search:`, `artist-aliases:`; C42) | the MusicBrainz client, as each call lands | the cache fill's tiers (`plan_run`), phase 4.1 |
| `mb_cache_fill_confirmations` (0058) | a later step's queue (4.4's add-only pass) | the cache fill's confirmations tier; its rows stay (4.4 owns their life cycle) |

- **Flows:**
  - upload → `ingestion_task` → `artist_matching_task` → `identity_matching_task`, with
    `ingestion_task` → `embedding_task` as a side branch that hands off to nothing (AUD-R020).
    A new line's version text needs no task: `track_identities.version_text` is generated
    (phase 2b S1), so the ingest upsert fills it
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
  - `backfill_artist_credits_task` (phase 2a S3): nothing queues it. The cutover and the gate run it
    once, by name, and it hands off to nothing
  - the cache fill (phase 4.1, D6, AUD-R091): nothing queues `mb_cache_fill_task`; a run is queued
    by name (the cutover runbook, then on Lance's word). It refuses while a run is in progress
    (a RUNNING `mb_cache_fill` row, or a batch in Huey's queue), else queues
    `mb_cache_fill_batch_task` at priority −10 → the next batch while capped work remains, and
    `rematch_names_task(names)` per tier the run leaves with names filled →
    `artist_matching_task` per playlist with pending work → `identity_matching_task`
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
    `staging.py` runs the branch's API (port 8020) and worker (no periodic tasks) on a copy.
    `regroup_dry_run.py --dsn <copy> --before <base checkout> --out <file>` (phase 2b S13)
    reads the copy the round trip leaves: every present file's base title and version by the
    base checkout's version reader and by this tree's; the files grouping would then place on
    another work or recording (predicted with grouping's own read-only `grouping_shortcut` and
    `planned_work`); and the MusicBrainz recordings whose next enrichment upsert would retype
    them into a version slot their work already holds, or that two of them are headed for
    (`uq_recordings_work_version`), which fails that upsert.
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
- **MusicBrainz is paced per machine, not per process** (AUD-R102). Every live call goes through
  `MusicBrainzApiClient._fetch`, which waits its turn on `services/mb_clock.py`'s file lock (1.1 s
  cadence, `%TEMP%/retrostation-musicbrainz.clock`). Artist lookups and library
  enrichment's recording search are batched 100 per request; the recordings phase's
  `lookup_recording` stays one call per recording because only it carries work relations.
- **The MusicBrainz cache is logged** (C42; AUD-R042, AUD-R090). 0057 made `mb_cache` LOGGED
  again (0020 had made it UNLOGGED, and a crash emptied the dev cache on 2026-11-06 and again on
  2026-11-11), because from phase 4.2 on matching reads only the cache. Reads filter on
  `expires_at > now()`, live clients keep rows 3,650 days (`mb_cache_ttl_days`), and the
  repository never commits: a caller commits the rows it wants to survive a restart.
- **The cache fill's client methods each have their own key prefix** (phase 4.1; AUD-R090):
  `search_recordings_by_name_title` (`recording-name-title:{words}:{title}`; every name word
  required, the title as a phrase; it keeps the hit count and each hit's score and whole credit
  with its joining words; a name with no words searches the title alone and says `title_only`),
  `search_artist_aliases_by_mbids` (`artist-aliases:{mbid}`, 100 artists per request, an unknown
  MBID cached as unknown) and `lookup_release_for_links` (`lookup_release` with a 404 remembered
  under `release-missing:{mbid}`; `lookup_release` itself caches no 404, because enrichment reads
  `release:` rows).
- **The cache fill works through tiers, in batches** (phase 4.1 PR 2; AUD-R091). `plan_run`
  (`services/cache_fill_service`) is a query: every call the run still has room for, in tier
  order, each within its cap per run (`services/cache_fill_constants`, from D5's count): payoff
  (names in review or auto rejected, a title a present file holds and C8 doesn't call common),
  C68 (untagged files with a joiner), C18 (a manual name's one artist, under the title song
  matching searches, so the row is song matching's), confirmations (0058), the tail (payoff's
  common titles, then titles no file holds), then the uncapped alias step (D3, `artist_names`
  rows written with their cache rows). A query with a live entry, or tried and not answered
  earlier in the run, is skipped, and one key is called once. `run_batch` makes at most 100 calls,
  none after 110 s, commits each as it lands, and ends the run at 5 transient failures in a row
  across its batches (the last row then says "stopped (MusicBrainz not answering)", D8); a
  permanent 4xx is logged and skipped for the run (M5), and 20 in a row with no answer between
  end the batch FAILED (`MusicBrainzRefusingError`: a block or a query bug, not one bad item).
  Name + title searches drop the log's f/ credit from the title (`credit_stripped_title`) and
  keep the rest as logged. The words come from
  `services/cache_fill_words` (`name_words`, `search_title`). The names re-check
  (`rematch_names_task`) rewinds as the re-check does, on its own task type, so the "changed"
  watermark never moves for it (S7, R6). Known gaps: a batch that dies loses the names its tier
  had filled, so they miss that re-check (Re-run Matching or 4.2 picks them up); and a crash's
  RUNNING row blocks new runs until the `/ws` reaper fails it, which needs a client listening.
- **A MusicBrainz outage defers the artist stage's cut-off names, never fails its run** (AUD-R028,
  extended by AUD-R042; AUD-R090). Until the old stage retires (4.3), `DeferOnMusicBrainzOutage`
  wraps the MusicBrainz strategy inside the cut-off gate: a transient failure of a cut-off name's
  live search (429 or 5xx after the client's retries, a timeout, a network error) makes the name
  `needs_review` / DEFERRED_RETRY, and after `ARTIST_MB_FAILURE_CUTOFF` (5) in a row the run
  defers its remaining cut-off names without a call; a permanent error still fails the run.
  `GET /api/v1/matching/mb-artists`, the one API route that calls MusicBrainz, answers 503 on any
  MusicBrainz error through `MusicBrainzUnavailableError` (`services/musicbrainz_search_service`).
- **Enrichment keeps a song's copies on one work** (AUD-R103). Every path that learns a
  recording's MusicBrainz work goes through one step in `library_enrichment_service`
  (`_performed_work`): the per-release and per-recording passes, the untagged pass and the
  recordings phase's lookup apply. When no work holds the MBID yet, the local work of the first
  linked file (by id) that is the same song (same artist, title equal by grouping's exact path,
  `grouping_service.exact_title_key`) takes it (`WorkRepository.claim_mbid`), so the copies
  enrichment cannot link stay with it. Where a work already holds the MBID, the file moves alone
  and the pair is left for review: works are never merged automatically. The recording is put
  on its work through the guarded `link_work`, so a held version slot leaves it off the work
  instead of raising. Grouping prefers the MusicBrainz work on an equal score.
- **A local work a move empties is deleted** (AUD-R103). `work_move_service._settle_work`
  first drops grouping's file-less recordings of it, then `delete_if_empty` decides as before:
  a match, a format override or another recording still keeps the work. MusicBrainz works keep
  their recordings.
- **Lifecycle reporting** stays in per-task envelopes (AUD-R011, AUD-R012): `task_run` for the
  enrichment pair, the local-artist linking, the matching re-check, the names re-check, each
  cache-fill batch and the artist credits backfill, `task_failure_telemetry`,
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
   master in one merge and adds rulings in each phase, so the next ruling on master is AUD-R104.

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
| `MatchRepository(?:\([^)]*\))?\.get_by_artist` `match(?:_repo\|es)\.get_by_artist` `get_by_artist\(.*\) -> Match\b` | the name's links, through `LogNameLinkService`; `WorkRepository.get_by_artist` was another method (retired in 3b PR 5b, AUD-R084) | 1c | #235 | AUD-R061 | `tests/matching_seed.py` |
| `artist_candidates` | the name's suggested links (the queue's `candidates`, 1c S8) | 1c | #235 | AUD-R062 | `tests/integration/test_matches_song_only_pg.py` `tests/routers/test_matching.py` `tests/routers/test_queue_suggested_links.py` |
| `resolved_artist_mbid` | none: read by nothing; a name's MBID is its linked artist's | 1c | #235 | AUD-R063 | `tests/integration/test_matches_song_only_pg.py` |
| `split_artist_string` | none: a name holding several performers gets one link each (M9), from evidence in phase 4 | 1c | #235 | AUD-R064 | none |
| `\breject_open_songs\b` | Reject's cascade (`plan_cascade`, phase 1b) | 1c | #235 | AUD-R065 | none |
| `\breopen_every_song\b` | Unmatch's cascade (`plan_cascade`, phase 1b) | 1c | #235 | AUD-R066 | none |
| `\bdelete_song_matches\b` | Unmatch's cascade, which deletes the changed lines' matches (phase 1b) | 1c | #235 | AUD-R067 | none |
| `resolution\.set_status\b` `ArtistResolutionRepository\.set_status\b` `def set_status\(self, broadcast_artist_id\b` | `LogNameLinkService` writes the status from the links (`status_from_links`, 1c S3) | 1c | #235 | AUD-R068 | `tests/services/test_status_from_links.py` |
| `links\.unlink_all\b` `LogNameLinkService\.unlink_all\b` `def unlink_all\(self, broadcast_artist_id\b` | `unlink_keeping_rejected` (Not an artist keeps the rejected pairs) | 1c | #235 | AUD-R069 | none |
| `link_copy_stubs` | none: 0046's rollback recreates it empty for 0044's rollback | 1c | #235 | AUD-R070 | `tests/integration/test_matches_song_only_pg.py` `tests/integration/test_log_name_links_copy_pg.py` |
| `\bwork_artist\s*[:=]` `\bcredit_mbids\b` | the performance's own credits: an untagged default carries its work's artist as a credit (3b D1), and the key compares `credit_artists` (AUD-R078) | 3b | PR 2 | AUD-R077 | none |
| `idx_works_artist` `\b(?:w\|fw\|gw)\.artist_id\b` | a work's artists are its performances' credits; 0055 keeps each existing work's artist in `work_artist_seed` for 3a's build and D1's step, which drops it (D38, D49) | 3b | PR 5b | AUD-R082 | `tests/audit/test_rulings_phase_3b_pr5b.py` `tests/integration/test_work_artist_seed_pg.py` |
| `idx_matches_work_id` `WorkReference\.MATCH\b` `\bfollow_file\b` `\brelease_work\b` `matches\.move_to_work\b` `\brelease_work_matches\b` | a line's work is its file's work; a match carries none | 3b | PR 5b | AUD-R083 | `tests/audit/test_rulings_phase_3b_pr5b.py` `tests/integration/test_work_artist_seed_pg.py` `tests/test_song_match_single_writer.py` |
| `\bget_by_artist\b` | `WorkRepository.works_crediting` (the works an artist's credits name); `library_files.get_by_artist_mbid` is another method and stays | 3b | PR 5b | AUD-R084 | `tests/audit/test_rulings_phase_3b_pr5b.py` `tests/matching_seed.py` `tests/integration/test_matches_song_only_pg.py` |
| `\bdelete_overrides_of_work\b` | none: an emptied work stays while an override points at it (D7) | 3b | PR 5b | AUD-R085 | `tests/audit/test_rulings_phase_3b_pr5b.py` |
| `\bdelete_recordings_of_work\b` | `delete_fileless_recordings_of_work` (D16) | 3b | PR 5b | AUD-R086 | `tests/audit/test_rulings_phase_3b_pr5b.py` |
| `dry_run_untagged_recordings` | none: its read before the untagged pass's first live run is done (D44) | 3b | PR 5b | AUD-R087 | `tests/audit/test_rulings_phase_3b_pr5b.py` |
