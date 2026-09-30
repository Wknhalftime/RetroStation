# Event-graph analyzer: output contract (Session A)

`scripts/audit/event_graph.py`. The tests in `tests/scripts/test_event_graph.py` are the
spec; this file records the mechanical definitions they assume. Status: PROPOSED until
the user rules at Gate A.

## Invocation

- stdlib only (`ast`, `argparse`, `json`, `re`, `pathlib`, ...). Never imports `backend`.
  No database, no network.
- `--root PATH` (default `.`). Every `.py` file under the root is parsed, except files
  under a directory named `tests` or one whose name starts with `.`, and under
  `node_modules`, `frontend`, `htmlcov`, `__pycache__`. Code under `tests` makes no
  edge, producer or hit.
- `--out PATH` (default `<root>/audit/event-graph.json`): always written.
- `--guard-fn MODULE:QUALNAME` (default `backend.tasks._enqueue_chain:enqueue_or_log`).
  ADDED OPTION: the fixture's guard lives elsewhere, and a same-named function in another
  module must not count as the guard (a name is never evidence).
- `--publish-fn`, `--subscribe-fn` (repeatable, `MODULE:QUALNAME`, default none).
- `--ev01-scope TASK` (repeatable, bare task names, default none).
- `--summary`, `--rule CODE`, `--node NAME`, `--chains`, `--check BASELINE`,
  `--limit N` (default 20), `--offset N` (default 0).
- Exit 0 clean, 1 new hits against the baseline (only with `--check`), 2 analysis error
  (e.g. the root does not exist; stderr names the root as given).

## Names

- module: POSIX path relative to the root, `.py` dropped, `/` -> `.`.
- qualname: `module:Outer.inner`; module-level code is `module:<module>`. A lambda is not a
  scope: calls inside it belong to the enclosing function.
- Imports resolve absolute and relative (`from ..tasks.x import t`, `from . import m`,
  `import a.b as m`), at module level or inside a function.
- Huey instance: module-level `NAME = <callee ending in "Huey">(...)` whose callee is
  imported from `huey`. Its module is the huey app. `results` is the keyword's literal
  value; absent means Huey's default, True.
- Task: a function decorated `@<instance>.task(...)` (kind `task`) or
  `@<instance>.periodic_task(...)` (kind `periodic`), the instance resolved through imports.

## Output (`schema_version: 1`)

`json.dumps(obj, sort_keys=True, indent=2) + "\n"`. Top-level keys: `schema_version`,
`header`, `nodes`, `edges`, `unresolved_dispatch`, `hits`, `counts`. No timestamps,
durations, host names or absolute paths; `file` values are POSIX relative to the root.

- `header.options`: every option. `root` is recorded as `"."`; other path options (`out`,
  `check`) by file name only; lists sorted.
- `nodes.tasks[]` sorted by (module, name): `name, module, file, line, kind, schedule`
  (source text of the periodic decorator's first argument, else null), `retries` (keyword
  literal, default 0), `envelope`, `body_lines`, `registered`, `params` [{name,
  annotation}].
  - envelope: a `with` item calling a function resolved to a def named `task_run` ->
    `task_run`; else `task_failure_telemetry` -> that; else a `try` with a handler in the
    body -> `own`; else `none`. Nested defs are not the task body.
  - body_lines = def `end_lineno` - first body statement `lineno` + 1.
  - registered: the task's module is imported by the huey app module.
- `nodes.producers[]` sorted by qualname: `qualname, layer, file, line` for every code
  location that is an edge producer. layer: function named `lifespan` -> `lifespan`; else
  the first directory segment of the file in {routers: router, tasks: task, services:
  service, scripts: script}; else `other`.
- `edges[]` sorted by (kind, producer, consumer); identity = (kind, producer, consumer).
  Repeated sites merge into one edge with sorted `lines`. Keys: `kind, producer,
  consumer, file, lines, guarded, message_class, args`.
  - ENQUEUE: `T(...)` where T resolves to a task (name, alias, or module attribute).
    `<guard-fn>(T, ...)` or `<guard-fn>(lambda: T(...), ...)` is one ENQUEUE, guarded
    true. guarded is true only when every merged site is guarded.
  - ENQUEUE_DEFERRED: `T.schedule(...)`. INLINE: `T.call_local(...)` or `T.func(...)`.
  - args: the consumer task's params [{name, annotation}] (annotation = `ast.unparse`, or
    null); [] for non-task consumers.
  - PUBLISH / SUBSCRIBE (consumer/producer endpoints are topics):
    - Huey signal: `@<instance>.signal(S1, S2)` handler H -> SUBSCRIBE
      `huey-signal:S` -> `module:H` per signal, and PUBLISH `appmodule:instance` ->
      `huey-signal:S` per distinct S. S is the identifier as written (no args -> `*`).
    - SQL text in a string literal: `\bNOTIFY\s+(\w+)` or `pg_notify\(\s*'(\w+)'` ->
      PUBLISH enclosing -> `pg-channel:<name>`; `\bLISTEN\s+(\w+)` -> SUBSCRIBE
      `pg-channel:<name>` -> enclosing. Case-insensitive.
    - `--publish-fn Q`: a call resolved to Q -> PUBLISH enclosing -> `topic:<first arg
      literal>`. `--subscribe-fn Q`: a call resolved to Q -> SUBSCRIBE `topic:<first arg>` ->
      handler (second arg resolved to `module:qualname`).
  - POLL: a `while`/`for` loop whose body (not nested defs) calls `sleep` (any receiver)
    and calls `.execute/.executemany/.fetchone/.fetchall/.fetchmany`. Producer = enclosing;
    consumer `table:<name>` from the first `FROM|UPDATE|INTO <name>` in a string literal
    in the loop, source order, else `table:?`.
  - message_class (derived, never judged): PUBLISH -> EVENT; ENQUEUE/ENQUEUE_DEFERRED ->
    QUERY if its result is read in the same function (`T(...).get(`, `T(...)(`, or the
    bound name later `.get(`-ed or called), else COMMAND; others null.
- `unresolved_dispatch[]` sorted by (file, line, reason, name): `file, line, producer,
  reason, name`. reason `getattr` (getattr on a module defining tasks; name null),
  `task_table` (a task name inside a dict/list/set/tuple literal; one row per task),
  `task_as_value` (any other load of a task name: argument to a non-guard call,
  assignment, return). Never dropped.
- `hits[]` sorted by (code, subject): `code, class, subject, file, line` plus extras.
  Identity = (code, subject). No severity.

| Code | class | subject | fires on |
|---|---|---|---|
| EV01 | ruling | `producer -> task` | ENQUEUE/DEFERRED, producer inside a task, guarded false. Extra `ruling_scope`: `stated` if the producer's task is in `--ev01-scope`, else `unstated` |
| EV02 | inventory | `producer -> task` | each INLINE edge (tests are never scanned) |
| EV03 | inventory | `module -> task` | a file under services/db/repositories/domain/playout that imports or calls a task; one per (module, task) |
| EV04 | inventory | `a -> b -> a` | each elementary cycle of the task graph (ENQUEUE/DEFERRED, producer task -> consumer task), rotated to start at the smallest name |
| EV05 | inventory | `producer -> task` / `appmodule:instance` | each QUERY edge; a Huey instance whose results is not literal False |
| EV06 | inventory | `module` / `module:task` | a module defining tasks not imported by the huey app (`detail: unregistered_module`); a task of kind task with no ENQUEUE/DEFERRED/INLINE edge in (`detail: no_producer`, extra `referenced_in_unresolved_dispatch`) |
| EV07 | inventory | `module:task(param)` | a param annotated with anything but str, int, float, bool, None, a union of those, or list[X] / dict[X, Y] with X, Y among them |
| EV08 | metric | `module:task` | every task; `value: {body_lines, service_calls}`; service_calls = distinct callees resolved to a def in a module with a `services` segment |
| EV09 | inventory | `producer` / `module:task` | each POLL edge; a periodic task with an `if`/`while` whose test contains a call or a name assigned in the task body |
| EV10 | metric | `producer` | every producer with an ENQUEUE/DEFERRED/INLINE edge; `value: {tasks: n}` distinct consumers |

`counts`: `node.task`, `node.periodic`, `node.producer`, `edge.<KIND>` (6), `class.COMMAND`,
`class.QUERY`, `class.EVENT`, `rule.EV01`..`rule.EV10`, `unresolved_dispatch`; zeros
included.

## CLI output

- `--summary`: one `key: value` line per `counts` key, sorted, plus the line
  `no publish mechanism found` when `class.EVENT` is 0. 40 lines or fewer.
- `--rule CODE`, `--node NAME`, `--chains`: listings capped by `--limit`; a truncated
  listing ends with `N more (--offset M)`. `--node` prints each in/out edge as
  `KIND producer -> consumer`. `--chains` prints the longest simple paths of the task
  graph and every cycle, as `a -> b -> c`.
- `--check BASELINE`: BASELINE is an earlier output; exit 1 if any current hit identity is
  not in it; lines are ignored.
