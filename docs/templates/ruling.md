# Ruling (our ADR)

A decision is recorded as one JSON line in `audit/rulings.jsonl` (ARCHITECTURE.md rule 3), not
in a separate ADR folder. Write one only for a decision someone would otherwise argue about
again: a rule the code must keep, an exception to an existing ruling, or a choice between
options that could come back.

## Draft it, then append it

Draft it in the design's "Decision and reasoning" section. Once Lance agrees, append it to
`audit/rulings.jsonl` as one line, with the keys in this order:

```json
{"id": "AUD-R0NN", "status": "PROPOSED", "date": "YYYY-MM-DD", "refs": [], "ruling": "…", "rationale": "…", "supersedes": []}
```

| Key | What goes in it |
|---|---|
| `id` | The next free `AUD-R` number. `AUD-###` ids without the `R` are audit findings. |
| `status` | `PROPOSED`, then `ACTIVE` once Lance agrees; `SUPERSEDED` once replaced. Left `PROPOSED` for more than a week, it counts as rejected. |
| `date` | The day it was proposed. |
| `refs` | The rulings and audit findings it builds on. |
| `ruling` | The rule, worded so a check can enforce it, ending with that check: a test (`tests/…::test_…`), a ruff code, an import-linter contract or `poe events-check`. A rule with no check gets deleted (ARCHITECTURE.md rule 2). |
| `rationale` | The evidence (a count, a profile, an incident, with its date), the options that lost and why, and who decided: "Lance chose (a) over (b) on YYYY-MM-DD". Link the design. |
| `supersedes` | Each ruling or written rule it replaces, and which part of it. `[]` if none. |

## Before appending

- [ ] It settles something; a line that only describes the code is not a ruling
- [ ] The ruling names the check that enforces it
- [ ] The rationale names the options that lost
- [ ] Every ruling it overrides is in `supersedes`, and anywhere ARCHITECTURE.md cites the old
      id now cites the new one too
