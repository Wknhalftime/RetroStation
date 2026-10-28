# Pre-coding templates

Four short documents, written in this order. Each one is written only when it changes a
decision. They let the builder, whether a person or an agent, build without guessing, and they
give review something written down to check the work against.

| Order | Template | Write it when | Size | Approved by |
|---|---|---|---|---|
| 1 | [problem-brief.md](problem-brief.md) | Someone asks for a feature or a change | One screen | Lance |
| 2 | [design.md](design.md) | Building takes longer than writing it up, or there are two or more ways to do it | Under 150 lines | Lance, before any code |
| 3 | [ruling.md](ruling.md) | A decision would otherwise be argued again | One JSON line | Lance (`PROPOSED` → `ACTIVE`) |
| 4 | [SPEC.md](SPEC.md) | Before every build | Under 120 lines | Lance; then the tests freeze |

A one-line fix goes straight to a SPEC, or just to a failing test. A design that settles
nothing new needs no ruling.

## The flow

1. **Brief.** The problem and who it hurts. No solution.
2. **Design**, if needed: the options, what each costs, and the choice. Approved before code.
3. **Rulings.** Each decision that must outlive the feature goes in `audit/rulings.jsonl`
   (ARCHITECTURE.md rule 3).
4. **SPEC.** The builder's whole brief: the requirements, the test that proves each one, and
   the commands that mean "done". If a row is blank, the builder asks before building.
5. **Tests first, then freeze.** The acceptance tests are written from the SPEC and checked
   against it before Lance locks them. A locked test changes only when a requirement changes.
6. **Build in a fresh session**, from the SPEC alone.
7. **Review against the SPEC:** every row, every edge case, and the pasted output of the
   definition of done.

## Two kinds of rule

A sentence in a document is guidance, and a reader (person or agent) can decide it does not
apply. A rule that must hold every time goes in a check: a test, ruff, mypy, import-linter,
`poe events-check` or a hook. The document then names the check. This is ARCHITECTURE.md rule 2:
a rule exists only if a check enforces it.

## Where filled-in copies go

Next to the existing specs: `docs/superpowers/specs/YYYY-MM-DD-<topic>-<brief|design|spec>.md`.
That folder is gitignored, so filled-in copies stay on this machine.

## Why this shape

- Problem and solution go in separate documents, the solution is approved before any code, and
  there is no design doc when building is quicker than writing one: HashiCorp's
  [PRD and RFC practice](https://hashicorp.com/how-hashicorp-works/articles/writing-practices-and-culture).
- A design records the options, the benefits and drawbacks of each, the reasoning and the
  decision, and timely beats polished: Oxide's [RFD 1](https://rfd.shared.oxide.computer/rfd/0001).
- Incomplete or hidden requirements were the most-cited requirements problem in a survey of 228
  companies (48%), for agile and plan-driven teams alike, and the authors recommend a
  completeness check before building ([NaPiRE](https://arxiv.org/abs/1611.10288)). Hence the
  SPEC's "Before building" list.
- A self-contained spec, a fresh session to build in, a pass/fail check for each task, evidence
  instead of "all tests pass", and review against the spec come from Anthropic's
  [Claude Code best practices](https://code.claude.com/docs/en/best-practices).
- Eval targets with numbers, across several dimensions, come from Anthropic's
  [success-criteria guide](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests).
  They are revised after grading real outputs because criteria drift
  ([Shankar et al., 2024](https://arxiv.org/abs/2404.12272)).
- The design's "What could make this fail" section is a pre-mortem. It is common practice, but
  the research behind these templates did not verify it.
