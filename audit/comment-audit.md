# Comment and doc audit (2026-10-05)

Every comment and docstring in tracked source (Python, TS/JS, SQL, TOML, YAML, Procfile;
`audit/` excluded) was indexed for citations: rulings (`AUD-R###`), findings (`AUD-###`),
decisions (`D##`), doc paths, line numbers and plan ids. Each citation was resolved against
`audit/rulings.jsonl`, the two findings ledgers, the tracked tree and `ARCHITECTURE.md`. A
one-off script did the indexing; it is not kept, per ARCHITECTURE.md rule 2.

## Fixed in this change

| what | sites | ledger |
|---|---|---|
| "AUD-R011 decision N" cited for decisions that are AUD-R012 (N) | 5 backend, 9 tests | AUD-066 PROPOSED-fixed |
| Line-number citations that had drifted (`websocket.py:57`, `matching.py:562-575`, `artist_matching_tasks.py:87-100`, ...) | 13 | AUD-077 PROPOSED-fixed |
| AUD-R017's Procfile comment did not exist; `huey_app.py` said "replace with RedisHuey for multi-worker" | Procfile, `tasks/__init__.py`, `huey_app.py` | AUD-065 PROPOSED-fixed |
| The three hand-offs outside `enqueue_or_log` described the bare call as design | `artist_matching_tasks`, `embedding_tasks`, `library_watcher_tasks` | now cite AUD-R014 / AUD-R020 and open AUD-059, 061..063 |
| AUD-R008 test comments still called Tier 1 auto-link "today's behaviour" (removed in PR #94) | `test_mb_enrichment_artist_snapshot`, `test_mb_enrichment_progress` | - |
| pyproject: redundant-expr "flags 9 lines" (0 since AUD-058; re-checked with mypy) and a citation of `retrostation-audit-kit.md` (not in the repo) | `pyproject.toml` | - |
| Rules cited only by a gitignored path (`.claude/CLAUDE.md`, `.claude/rules/*.md`, `error-handling.md`, a deleted plan doc): rule restated or tied to ARCHITECTURE.md | 2 migrations, 1 rollback, `mb_enrichment_tasks`, `routers/matching`, `matching_constants`, 5 tests | - |
| `routers/settings.py` "tracked debt" pointing at an untracked follow-up task: now cites AUD-R010 | 1 | - |
| Characterisation-test docstrings written as if their refactor had not landed (AUD-014, AUD-015/040) | 2 | - |

Every `AUD-R###` and `AUD-###` cited in code now resolves. No code cites a SUPERSEDED ruling.
The ARCHITECTURE.md facts were re-checked and hold: 133 router `.execute()` calls in 10 files,
1,453 / 1,101 lines, 7 baseline ignores, watcher every 4 min, the two resumes every 5 min,
`/ws` polls every 0.5 s, the event-graph gate not wired into CI.

## Open: needs an owner's decision

1. **The streaming decision register is not in the repo.** About 1,300 citations of `D1`..`D109`
   (in 227 files) plus plan ids (`plan-f2.md`, `traceability-f2.md`, `final-review.md`,
   `PR A`..`PR G2`, `T4.2`, `I1`, `PG2`, `M5`) point into `docs/superpowers/`, which
   `.gitignore` excludes. Nothing in the repo can check them, and rule 3 says decisions live in
   `audit/rulings.jsonl`. Options: commit the streaming spec (or just its decision table), or
   move the live D-decisions into the ledger.
2. **PROPOSED rulings older than a week.** AUD-R001, R002, R003 and R005 (2026-09-26) count as
   rejected under rule 3, but the ledger has no status for that. They are left PROPOSED.
3. **`.claude/frozen-tests.json`** is cited by 9 tests as the frozen-test guard. It is a local
   hook (ARCHITECTURE.md says so) and is not in the repo.
4. **Test names** `test_tier1_high_confidence_auto_links_mbid` and
   `test_tier1_low_confidence_marks_enhanced_without_mbid` describe pre-AUD-R008 behaviour.
   Renaming them also renames their snapshot keys, so they are left for a test change.
