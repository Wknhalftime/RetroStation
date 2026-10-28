# <Topic> — SPEC (YYYY-MM-DD)

**Status:** Draft | Ready (Lance, YYYY-MM-DD) | Built (PR #…)
**Brief:** <link> · **Design:** <link, or "none"> · **Rulings:** AUD-R… · **Branch:** feat/…

<!-- This file is the builder's whole brief. A person or an agent must be able to build from it
     in a fresh session without the conversation that produced it. Keep it under 120 lines.
     Delete these comments when done.

     Agent reading this: if any row is blank or says TBD, stop. Ask Lance multiple-choice
     questions until it is filled in; do not fill a gap with a guess. -->

## Context the code cannot give you

<!-- Only what reading the repo would not tell a builder: a decision made in conversation, an
     old pattern not to copy, a measured limit. Not a tour of the code. -->

## Requirements

| ID | The finished feature… | Proved by |
|---|---|---|
| S1 | <one observable behaviour> | `tests/…/test_….py::test_…` |
| S2 | | |

<!-- One behaviour per row, stated as something a user or another component can observe. Every
     row names its test. A row no test can prove is a wish: rewrite it or drop it. -->

## Edge cases and failures

| ID | When… | Then… | Proved by |
|---|---|---|---|
| E1 | | | |

## Out of scope

<!-- What the builder must not do, even if it looks like an improvement. -->

## Before building (Definition of Ready)

The SPEC is Ready when each line is answered in a row above, or marked n/a with a reason.

- [ ] Limits: sizes, counts and timeouts, and what happens past each one
- [ ] Failure: MusicBrainz down or slow (1.1 s per request), database slow, a file missing or
      silent, Liquidsoap restarted
- [ ] Repeats: a message delivered twice, a button pressed twice, a restart halfway through
- [ ] Empty: no files, no plays, no station, nothing pending
- [ ] Existing data: rows written before this change, and whether a migration must touch them
- [ ] Time: `played_at` is station wall-clock, not UTC
- [ ] Paths: Windows paths; never `LIKE` on a path (backslash is its escape), use `starts_with()`
- [ ] Interfaces: migration, API route, Huey message, `/ws` progress, settings; each one named,
      or "none"
- [ ] UI: the loading, empty, error and in-progress states
- [ ] Frozen tests this changes, each with the ruling that forces the change

## Acceptance tests (written first, then frozen)

<!-- New test files, written from the rows above and checked against them before Lance locks
     them. For each locked test this changes, give the ruling that forces it and whether it
     adds or removes assertions. A locked test changes only when a requirement changes, never
     to fit the code. -->

## Eval targets (only for a feature that calls a model)

| Dimension | Measure | Target | Data | Command |
|---|---|---|---|---|
| Quality | | | | |
| Safety | | | | |
| Latency | | | | |
| Cost | | | | |

<!-- Delete this section if nothing calls a language or embedding model. The targets are a first
     draft: revise them after grading real outputs, and date each revision here. -->

## Definition of done

Done means every command below exits 0 on the branch, and the PR body pastes each one's summary
line. "All tests pass" on its own is not evidence.

```powershell
uv run pytest <acceptance test paths> -n 0   # S1..Sn, E1..En; DATABASE_URL = your own test DB
uv run pytest -m "not integration and not slow"
uv run ruff check .
uv run ruff format --check .
uv run mypy backend --strict
uv run lint-imports
uv run poe events-check
# only if frontend/ changed:
Push-Location frontend; npm run typecheck; npm test; Pop-Location
```

- [ ] ARCHITECTURE.md matches the code, or no section of it changed
- [ ] Every new decision is a ruling in `audit/rulings.jsonl`
- [ ] A reviewer in a fresh session checked the diff against every S and E row
