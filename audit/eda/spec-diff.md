# Spec expectations: mine vs independent derivation

Mine: `spec-expectations.jsonl` (68 rows). Independent: `spec-expectations-independent.jsonl`
(68 rows, fresh-context subagent given only the owner's contract text, the fixture tree and
neutral case ids K01-K68; the id map is `case-key.json`). Not resolved by me.

## Agreement

All EV01-EV09 hit sets and counts; EV10 values except the tests/ producer (D1); every
message class (COMMAND 23, QUERY 1, EVENT 5); ENQUEUE 23, ENQUEUE_DEFERRED 1, PUBLISH 5,
POLL 1; EV06 includes `table_only_task`; alt `results` omitted counts as enabled; merged
fan_out edge; guard by lambda and by value; same-named fake guard is not the guard.

## Disagreements

| # | Case (id) | Mine | Independent |
|---|---|---|---|
| D1 | EV02-miss (K07), TOTALS, EV10 | code under a `tests` dir is not scanned: no edge, no producer | INLINE edge `evgapp.tests...:check_tail -> chain_tail_task` recorded, only EV02 suppressed: INLINE 4, EV10 24 |
| D2 | EVT-signal (K32), TOTALS | PUBLISH `evg_huey_app:huey -> huey-signal:S` and SUBSCRIBE `huey-signal:S -> on_task_done`, per signal (2 + 2) | PUBLISH lifecycle -> `on_task_done` per signal (2), one SUBSCRIBE for the handler: SUBSCRIBE 3 |
| D3 | UD-table (K47) | one row per task in the table (2 rows at line 21): unresolved 4 | one row per table: unresolved 3 |
| D4 | EVT-fn (K35) | SUBSCRIBE `topic:batch.finished -> evg_bus:on_batch_finished` (the handler) | SUBSCRIBE by `evg_bus:wire` (the function calling subscribe) |
| D5 | TOTALS (K52) node.producer | 28: code locations with ENQUEUE/DEFERRED/INLINE/PUBLISH/POLL edges, incl. the Huey instance; subscribers are consumers | 24 task-naming producers (incl. check_tail) + announce_batch, wire, announce_import, announce_scan, listen_imports, watch_progress, on_task_done |
| D6 | EV01-hit (K01) | in-scope hit carries `ruling_scope: stated` | in-scope hit simply lacks `unstated` (marking unspecified) |
| D7 | NODE (K50) | schedule = decorator argument source text `crontab(minute="*/4")` | `crontab(minute='*/4')`; flags the encoding as unstated |
| D8 | EXIT-2 (K61) | exit 2 and stderr names the missing root | exit 2 only |

D8 is mine by necessity: exit 2 alone passes before the analyzer exists (Python exits 2
when the script file is missing), so the test also asks for the root in stderr.

## Anchor ambiguity, not disagreement

K53-K56 (DET-bytes, DET-root, DET-format, HEADER) and K62-K63 (SRC-nofixture, SRC-stdlib)
had anchors too similar to tell apart; the subagent's contents match the tests but land on
shuffled ids.
