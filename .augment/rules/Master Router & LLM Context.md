---
type: "always_apply"
description: "Example description"
---

# AGENTS.md — Master Router & LLM Context Architect

> **This file is the single entry point for any AI coding assistant operating in this repository.**
> Read this file first. Then load only the rule files relevant to the task at hand.

---

## Repository High-Level Goal

This codebase is a **Python-first, design-principled software system** built around the values of:
- **High cohesion, low coupling** — every unit does one thing and owns its own dependencies
- **Ports & Adapters architecture** — domain logic is framework-free and independently testable
- **Explicit over implicit** — no magic, no global state, no singleton anti-patterns
- **Evolutionary design** — the codebase must remain easy to extend without requiring rewrites

The primary audience for this codebase is developers who care deeply about maintainability, testability, and architectural integrity over speed-of-hack. AI-generated code must match this bar.

---

## Rule File Map

Use this table to determine which rule files to load for a given task. **Load only what is relevant.** Combining 2–3 rule files for a complex task is expected and correct.

| Situation / Signal | Load This Rule File |
|---|---|
| Writing a new class, deciding inheritance vs composition, adding a `@dataclass`, creating Value Objects | [`oop-class-design.md`](./oop-class-design.md) |
| A function imports multiple unrelated libraries, a class creates its own dependencies, "inappropriate intimacy" with another object's internals | [`coupling-cohesion.md`](./coupling-cohesion.md) |
| Building an API endpoint in FastAPI/Flask, creating a service layer, structuring a new module or feature | [`architecture-structure.md`](./architecture-structure.md) |
| Adding `try/except`, designing error responses, building retry logic, defining custom exceptions | [`error-handling-resilience.md`](./error-handling-resilience.md) |
| Long `if/elif` chains, need for Observer/Strategy/Repository/CQRS patterns, Singleton spotted in code | [`design-patterns.md`](./design-patterns.md) |
| Task is "clean up", "simplify", "refactor legacy code", function is too long, duplicated code spotted | [`refactoring-code-quality.md`](./refactoring-code-quality.md) |
| Any LLM/AI agent pipeline, multi-step prompt chains, tool use, structured output from an LLM | [`ai-agent-integration.md`](./ai-agent-integration.md) |

---

## Cross-Cutting Defaults (Always Active)

These rules apply to **every task**, regardless of which domain rule files are loaded:

1. **Functions first, classes second.** Default to a function. Introduce a class only when state must be maintained between calls or an entity must be represented.

2. **One sentence, no "and".** Every function and class must be describable in a single sentence without the word "and". Violators get split.

3. **No `except Exception` bare catches.** All exception handling must catch a specific type. Bare `except` and `except Exception` are prohibited except at the absolute top-level error boundary.

4. **Type hints everywhere.** All function signatures, return types, and class attributes must carry type hints. Use `from __future__ import annotations` for forward references.

5. **Tests before refactoring, tests alongside new code.** No production code change is submitted without a corresponding test that would have caught a regression.

6. **Design for the next developer — and the next AI assistant.** Folder structure, naming, and module boundaries must make the system's intent obvious to any reader with zero context. The harder a new agent needs to work to understand a module, the worse its output will be.

---

## Quick Diagnostic Checklist

Run this checklist before generating any new code or refactoring existing code:

```
□ Does this class/function have exactly ONE reason to change?
□ Does it import only what it genuinely needs?
□ Is it creating its own dependencies (smell) or receiving them (correct)?
□ Can I describe it in one sentence without "and"?
□ Is there a test that would break if this broke?
□ If I add a new variant/type/strategy, do I edit existing code or add a new file?
□ Is domain logic isolated from framework code (FastAPI, SQLAlchemy, etc.)?
□ Are exceptions typed, layered, and translated at the correct level?
```

If any answer is "no", consult the relevant rule file above before proceeding.

---

## Anti-Pattern Quick Reference

| Anti-Pattern Spotted | Immediate Action |
|---|---|
| `if isinstance(x, TypeA): ... elif isinstance(x, TypeB):` | Load `design-patterns.md` → apply Strategy |
| `self.smtp = SMTPLib()` inside `__init__` | Load `coupling-cohesion.md` → inject dependency |
| `raise HTTPException` inside a `domain/` file | Load `architecture-structure.md` → move to adapter |
| `except Exception: pass` or bare `except:` | Load `error-handling-resilience.md` → type the catch |
| `class Foo(Bar)` where only 2 of Bar's 10 methods are used | Load `oop-class-design.md` → compose instead |
| `singleton_instance = None; def get_instance():` | Load `design-patterns.md` → use module or injection |
| Prompt + LLM call + DB write in one function | Load `ai-agent-integration.md` → split into chain |
| Refactoring without first running tests | Load `refactoring-code-quality.md` → safety net first |

---

*This file is the map. The rule files are the territory. Always read the map first.*