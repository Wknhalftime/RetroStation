# Event-graph audit: Session C report (2026-10-04)

## 1. Verdict

Tool status clears the bar (precision 15/15, recall reconciled with no unexplained difference), so every criterion is measured. The background work is a **command pipeline, not event-driven**: all 14 messages are COMMANDs to one named consumer (0 EVENT, 0 QUERY, fan-out ≤ 2), a split unchanged since April (7 → 12 → 14 enqueues). About eight status columns carry the real coordination and make duplicate delivery harmless, but only because both Huey consumers run `-w 1`. The defects are command-pipeline defects: three hand-offs that breach AUD-R014, a worker write that overwrites a manual match, and embedding on the matching path although nothing reads the vectors. **Decide first:** ratify shape (b), a command pipeline named and documented as such (AUD-R015), because the fixes and the gate below assume it.

## 2. Tool status (state.json `tool_status`, copied)

| item | value |
|---|---|
| analyzer | 715d5e5 `scripts/audit/event_graph.py`; spec 56 passed first attempt; ruff clean; mypy --strict clean; no suppressions |
| precision | seed 20261003, 10 edges + 5 hits: before fixes 15/15, after fixes 15/15 (no fix needed) |
| recall | tasks 16, instances 2, ENQUEUE 14, guarded 5, INLINE 3, POLL 1, EVENT 0: all match the inventory; `tool_gaps: none found in recall` |
| mutation | 1676 mutants: 1091 killed, 584 survived (249 equivalent, 287 rule-logic spec gaps GAP-02..18, 48 plumbing) |
| known gaps | 18 in spec-gaps.jsonl, not yet ruled; GAP-01: cue tasks inside `with reported_failures(...)` report envelope `none` |
| re-run (Session C) | `--check audit/event-graph.baseline.json` exits 0 on HEAD ad7f317 and on master a97a64a; counts and edges identical |
**Caveat:** 287 surviving rule-logic mutants mean rule branches that this code does not exercise are untested. The real-code numbers rest on the recall and precision checks above, not on the mutation score.

## 3. Conformance

C1–C8 are not defined in any repo file I could find (audit/, docs/, the main checkout's .superpowers/, git log). The definitions below are reconstructed from the Phase 4 brief and the EV rule codes (open question 1). "Tool" = counted by the analyzer; "judged" = spot reads (section 10 has the budget).

| C | criterion (measure) | value | status | rules | source |
|---|---|---|---|---|---|
| C1 | messages announce facts (EVENT share of classified edges) | 0 / 14 EVENT; 0 PUBLISH/SUBSCRIBE | BROKEN for EDA; expected under (b) | class | tool |
| C2 | every task→task hand-off is guarded (unguarded task-produced enqueues) | 3 of 8 (AUD-061/062/063) | BROKEN (AUD-R014) | EV01 | tool |
| C3 | queue not bypassed; every task has a producer | INLINE 3 (2 periodic reconcilers, 1 bench script, all deliberate); EV03 0; EV06 1 dead task | BROKEN (EV06 only) | EV02 EV03 EV06 | tool + judged |
| C4 | second delivery harmless (top-5 inbound) | 5/5 harmless serially; none safe beyond `-w 1` | KEPT, conditional on AUD-R017 | — | judged |
| C5 | id payloads survive state moving on | every id or scope payload re-reads under a status filter; 1 blind overwrite (AUD-060); EV07 1 (50 MB bytes) | BROKEN (1 site) | EV07 | tool + judged |
| C6 | no poll where a message could replace it | 2 EV09 + 2 periodic resumes; all 4 kept on merit | KEPT | EV09 | tool + judged |
| C7 | no request/response or cycles over the queue | EV04 0, EV05 0 (both instances `results=False`), QUERY 0 | KEPT | EV04 EV05 | tool |
| C8 | thin handlers (top-3 EV08: envelope / logic, lines) | scan_files 190/62, ingestion 239/8, lib_enrichment 105/65 | BROKEN (watcher holds logic) | EV08 | tool + judged |

C4 top five by inbound edges: library_enrichment_task 4, artist_matching_task 2, identity_matching_task 2, library_hash_backfill_task 2, mb_enrichment_task (tie-break: 4 upstream paths).

## 4. Message mix

| layer | EVENT | COMMAND | QUERY | INLINE | POLL |
|---|---|---|---|---|---|
| router | 0 | 5 | 0 | 0 | 0 |
| task | 0 | 9¹ | 0 | 2 | 0 |
| script | 0 | 0 | 0 | 1 | 0 |
| other (websocket) | 0 | 0 | 0 | 0 | 1 |
| service / lifespan | 0 | 0 | 0 | 0 | 0 |

¹ Includes `request_cue_analysis`, a plain function that StreamingService reaches through a CueReporter callback wired in `main.py:202`. That is injection, not a services→tasks import, so EV03 stays 0.

Most-coupled producers (EV10): `library_scan_task` 2. All 15 others have 1, so there is no tenth to rank: `upload_playlist`, `scan_library`, `_enqueue_playlists`, `resolve_artist`, `retry_enrichment`, `artist_matching_task`, `embedding_task`, `ingestion_task`, `library_enrichment_task`, …

## 5. Flow map (`--chains`; [g] guarded, [u] unguarded, ⇒ inline, ⟲ poll, {column} = hidden channel)

```
path  ingestion_task -> embedding_task -> artist_matching_task -> identity_matching_task
path  library_watcher_poll -> library_scan_files_task -> library_enrichment_task -> mb_enrichment_task
upload_playlist(router) -> ingestion_task -[g]-> embedding_task -[u]-> artist_matching_task -[u]-> identity_matching_task
_enqueue_playlists(router) -> artist_matching_task {match_status pending, DEFERRED_RETRY}
resolve_artist(router) -> identity_matching_task {match_status reset to pending}
library_watcher_poll(*/4 min) {library_folder_staged_hashes} -[u]-> library_scan_files_task -[g]-> library_enrichment_task
scan_library(router) -> library_scan_task -[g]-> library_hash_backfill_task {audio_hash IS NULL}
                                          -[g]-> library_enrichment_task {enrichment_status} -[g]-> mb_enrichment_task {needs_enhancement}
retry_enrichment(router) -> library_enrichment_task          library_hash_backfill_resume(5 min) ⇒ library_hash_backfill_task
StreamingService -> request_cue_analysis -> stream_cue_request_task   stream_cue_analysis_resume ⇒ stream_cue_analysis_task {no stream_cues row}
websocket_endpoint ⟲ progress_tracking (0.5 s per connection)        cycles: none (EV04 0)
```

## 6. Top findings (all 22 are in `audit/eda/findings.jsonl`; ids continue from AUD-058)

| # | id | sev | file:line | finding | principle |
|---|---|---|---|---|---|
| 1 | AUD-059 | MED | tasks/embedding_tasks.py:68 | embedding failure stops all matching, yet nothing reads the vectors | chain only on a data dependency |
| 2 | AUD-060 | MED | db/repositories/broadcast_artists.py:108 | worker blind UPDATE overwrites a manual match the API made mid-run (`resolve_artist` has no status guard, routers/matching.py:638) | stale read; guard on the read status |
| 3 | AUD-061 | MED | tasks/artist_matching_tasks.py:110 | unguarded hand-off → identity_matching | AUD-R014 |
| 4 | AUD-062 | MED | tasks/embedding_tasks.py:68 | unguarded hand-off → artist_matching | AUD-R014 |
| 5 | AUD-063 | MED | tasks/library_watcher_tasks.py:102 | unguarded hand-off; a failure strands folders for the 1 h TTL | AUD-R014 |
| 6 | AUD-064 | MED | tasks/library_watcher_tasks.py:106 | 62 lines of scan/group/reconcile logic inside the fattest task | thin handler |
| 7 | AUD-065 | LOW | Procfile:2 | `-w 1` is what makes 4 tasks duplicate-safe; undocumented; MB limiter is process-local | make the constraint explicit |
| 8 | AUD-066 | LOW | tasks/_enqueue_chain.py:9 | 5 comments cite AUD-R011 "decision 1" for the hand-off rule (it is AUD-R012 (1)) | ruling citations |
| 9 | AUD-067 | LOW | db/repositories/library_files.py:289 | enrichment result UPDATE not guarded on `pending` | guard on the read status |
| 10 | AUD-068 | LOW | websocket.py:64 | progress poll also reaps stale rows: N tabs = N reapers, no tab = no reaper | CQRS |
| 11 | AUD-069 | LOW | tasks/normalize_backfill_tasks.py:50 | dead task (EV06); payload is a DSN | no orphan consumers; no secrets in messages |
| 12 | AUD-070 | LOW | routers/library/scan.py:39 | double-click queues a second full rescan | dedupe at the producer |
| 13 | AUD-071 | LOW | tasks/library_hash_backfill_tasks.py:136 | unhashable rows make the 5-min resume re-walk forever | poll should see zero work |
| 14 | AUD-072 | LOW | tasks/ingestion_tasks.py:202 | 239/247 lines are a hand-rolled envelope (logic already in the service) | thin handler (R011 limits the fix) |
| 15 | AUD-073 | LOW | routers/ingestion.py:37 | up to 50 MB of `file_bytes` pickled into the queue (EV07) | claim check |

Kept on the evidence: **polls** (the watcher poll, both periodic resumes and the websocket poll are correct at this scale; none has a message that could replace it without losing crash recovery) and **passive-aggressive events** (none: `scan_completed`, `batch_stored`, `on_row_processed` are logs or in-process callbacks with no required consumer).

## 7. Breaches of rulings in force

- **AUD-R014 (ACTIVE):** 3 unguarded hand-offs, at artist_matching_tasks.py:110, embedding_tasks.py:68 and library_watcher_tasks.py:102 (AUD-061..063). EV01 history: 4 → 7 → 3.
- **Citation mismatch:** `_enqueue_chain.py:9`, `ingestion_tasks.py:351`, `library_enrichment_tasks.py:234`, `library_scan_tasks.py:531` and `library_watcher_tasks.py:307` cite "AUD-R011 (decision 1)" for "the caller owns the handoff". The ledger's AUD-R011 rules only which tasks share `task_run` and has no numbered decisions. The text they paraphrase is AUD-R012 (1) (AUD-066).
- **Checked, no breach:** AUD-R008 (mb_enrichment_tasks.py:107, :484 match the ledger); AUD-R011 (enrichment pair `task_run`, scan/watcher/ingestion `own`, hash backfill `task_failure_telemetry`); AUD-R012 (2)/(3) for the watcher (SystemLogs :164, :287, :349); AUD-R012 (1) for its four named producers (all via `enqueue_or_log`).

## 8. The decision for you

| | what changes | cost | what it fixes |
|---|---|---|---|
| (a) events for announcements + commands on the critical path | a `library_files_changed` fan-out after scan/watcher commits (consumers: hash backfill, enrichment) and a `playlist_ingested` fan-out (embedding, artist matching) | S–M; still commands underneath (Huey has no pub/sub) | the watcher→hash-backfill gap (today the 5-min resume covers it) and AUD-059; adds a vocabulary of facts no one else consumes |
| (b) command pipeline, named honestly | docstring contract of the status columns (R015), `-w 1` constraint (R017), guarded writes (R018), 3 EV01 fixes, embedding side branch (R020), gate (R019) | S: about 6 small PRs, no new mechanism | every finding above; makes the gate's EVENT = 0 expected rather than alarming |
| (c) events throughout | Huey signals or NOTIFY/LISTEN; producers stop naming consumers | L: a LISTEN connection in the async API, choreography for the ordered ingest→match flow, idempotency already exists | nothing found today. Ingest → artist → identity must run in order and commits before each hand-off: a command is correct there |
**Recommendation: (b).** Name and document the command pipeline. Borrow (a)'s one fan-out helper only if the watcher ever needs the hash backfill sooner than 5 minutes.

## 9. Proposed gate (NOT applied)

```diff
--- a/pyproject.toml
+++ b/pyproject.toml
@@ [tool.poe.tasks]
 hotspots    = "python scripts/audit/hotspots.py --since '12 months ago' --out audit/hotspots.json"
-audit.sequence = ["lint", "lint-stats", "types", "layers", "deadcode", "deps", "vulns", "tests", "hotspots"]
+# Event graph (AUD-R019). EV01 hit identity ignores --ev01-scope, so the check needs no scope list.
+events          = "python scripts/audit/event_graph.py --summary --out audit/event-graph.json"
+events-check    = "python scripts/audit/event_graph.py --check audit/event-graph.baseline.json --out audit/event-graph.check.json"
+events-baseline = "python scripts/audit/event_graph.py --out audit/event-graph.baseline.json"
+audit.sequence = ["lint", "lint-stats", "types", "layers", "deadcode", "deps", "vulns", "tests", "hotspots", "events"]
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ jobs: lint: steps:
       - run: uv run lint-imports
+      - run: uv run poe events-check
--- a/.pre-commit-config.yaml
+++ b/.pre-commit-config.yaml
@@ hooks:
+      - id: event-graph
+        name: event-graph --check (AUD-R019)
+        entry: uv run poe events-check
+        language: system
+        files: ^(backend|scripts)/.*\.py$
+        pass_filenames: false
--- a/.gitignore
+++ b/.gitignore
+audit/event-graph.check.json
```

`events` drops the 16 `--ev01-scope` names. Only `ruling_scope` changes, and AUD-R014 makes every task in scope. Keep them in the poe task if the stored JSON should match today's byte for byte.

## 10. Open questions (each with my recommended answer)

1. **C1–C8 definitions.** Not found in the repo. *Recommend:* accept section 3's definitions and store them in contract.md.
2. **Are embeddings meant to be read?** The HNSW indexes in 0005 suggest semantic matching was planned. *Recommend:* if not on the roadmap, apply AUD-R020; keep the vectors and the task.
3. **Huey signals** (deferred from Session B). *Recommend:* no (AUD-R016); D2 stays as written.
4. **Will either consumer ever run more than one worker?** *Recommend:* no; record AUD-R017 and add locks only on that change.
5. **Gate noise.** EV08/EV10 are metrics: every new task or producer is a new hit identity, so the PR that adds it also refreshes the baseline. *Recommend:* accept, since that is the review moment. The alternative (`--check` on EV01–07/09 only) is a spec change.
6. **18 spec gaps** (GAP-01, 05, 10, 16, 18 need a ruling). *Recommend:* rule GAP-01 first (recognise `reported_failures`); it is the only gap that touches a real-code value.

**Left out:** EV08 splits beyond the top three (the brief caps it); re-sampling precision (Session B's sample stands, the re-run is identical); frontend consumers of `/ws` (not in the task graph). NOT MEASURED: AUD-080 watcher lock vs full scan (about 2 windows, moot under `-w 1`) and whether the review UI offers PENDING artists (AUD-060 reachability, about 1 window, matching.py:205-232).

**Read budget:** 7 of 25 windows in my context. Seven module subagents used 5, 5, 8, 7, 7, 7 and 4 windows (cap 8 each). Greps are not counted as windows.
