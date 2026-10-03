# D1-D8 best-practice review (2026-10-03)

Fresh-context subagent, read-only, after Lance accepted D1-D8 as written. Reviewer read
contract.md, spec-diff.md, parts of the tests and fixtures; no web search (general
knowledge of the tools cited).

| # | Verdict | Reasoning | Smallest change offered |
|---|---|---|---|
| D1 tests not scanned | BEST PRACTICE | dead-code tools (vulture) run without tests so test calls do not mask unused code; no production code lives under a `tests` dir today | record the excluded dir names in `header.options` |
| D2 signal as topic node | CONCERN | channel shape is standard (AsyncAPI, event storming), but Huey lifecycle signals are framework notifications; counting them as EVENT inflates the measure. Zero effect today (no `.signal(` in backend). `@signal()` with no args makes a `huey-signal:*` topic unconnected to specific ones | tag framework edges `origin: framework` and count them apart from `class.EVENT` |
| D3 one row per table entry | BEST PRACTICE | lets EV06 attribute `referenced_in_unresolved_dispatch` per task; unresolved rows are outside `--check` | none |
| D4 subscriber = handler | BEST PRACTICE | consumer is the code that reacts; the subscribe() caller is wiring | unresolvable handler (lambda, `self.on_x`, partial) -> `unresolved_dispatch` reason `subscribe_handler` |
| D5 producer count | ACCEPTABLE | follows from D1/D2/D4; Huey instance is a producer only because of D2 | exclude framework PUBLISH from `node.producer` |
| D6 `ruling_scope: stated` | BEST PRACTICE | always-present closed enum (SARIF / JSON Schema practice) | none |
| D7 schedule source text | ACCEPTABLE | source text varies with formatting; `ast.unparse` is canonical; low risk, not in `--check` | name the function in the contract (`ast.get_source_segment` of the first positional arg) |
| D8 exit 2 + stderr | BEST PRACTICE | ruff/mypy/argparse convention; stderr check defeats the vacuous pass | none |

Whole-spec observations:
1. `--check` gates hits only; `unresolved_dispatch` can grow silently. Change: exit 1 when it
   grows, or compare rows by (reason, producer, name).
2. Unparseable / unreadable files are unspecified. Change: exit 2 naming the file, or list
   them in `header.parse_failures`.
3. Renames show as one new hit plus one silently vanished hit. Change: `--check` prints
   "N baseline hits no longer present".

My note on D2: the owner's contract itself defines a Huey signal handler as SUBSCRIBE and the
lifecycle as PUBLISH, and EVENT as a PUBLISH edge. The concern is with that mechanism (1), not
with D2's topic shape; it needs the owner's ruling, not a spec edit on my initiative.

Status: none of the changes applied. Awaiting Lance's ruling before the lock.
