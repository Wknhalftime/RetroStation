---
type: "agent_requested"
description: "Auto-load when any of the following are detected:  Task description includes:"
---

# Rule Domain: Refactoring & Code Quality
**Name:** `refactoring-code-quality.md`

## Trigger
Auto-load when any of the following are detected:
- Task description includes: "refactor", "clean up", "simplify", "improve", "legacy code"
- File contains functions longer than ~30 lines
- Keywords: `# TODO`, `# FIXME`, `# HACK`, or commented-out code blocks
- A function name contains "and", "or", or "also" (multi-responsibility smell)
- Deeply nested `if` blocks (3+ levels of indentation)

---

## Critical Constraints

### 1. Write the Full Test Suite Before Changing a Single Line of Production Code
Refactoring without a test suite is rearranging furniture in the dark. Before touching the code: (1) write tests that cover the *current* observable behavior, (2) run them and confirm they pass, (3) only then begin refactoring. Tests are the safety net; a half-deployed net is as dangerous as no net.

> **The Why:** The only reliable measure that a refactor preserved behavior is a test suite that passed before and passes after. Coverage percentage is a lagging indicator — the tests must assert correctness, not just execution.

### 2. Refactor in the Sequence: Rename → Extract → Restructure → Generalize
Never jump straight to architectural patterns. Start with cosmetic clarity (rename magic strings to constants, rename misleading variables). Then extract sub-functions. Then restructure the call graph. Only introduce patterns (Strategy, Repository) after the code is already clean enough to see the shape.

> **The Why:** Jumping to a pattern before cleaning reveals the wrong abstraction boundary. Incremental steps keep the codebase working at every intermediate stage and make each decision visible and reversible.

### 3. Tolerate Temporary Duplication During Refactoring
It is correct to introduce a duplicate intermediate structure during refactoring (e.g., temporarily having both an `if/elif` chain and an emerging class hierarchy). Premature removal of duplication before the new structure is proven is the #1 cause of "refactoring hell."

> **The Why:** Removing duplication before understanding the correct abstraction creates wrong abstractions that are worse than duplication. Two similar things become one thing only after you understand *why* they are similar.

### 4. Code Duplication Is Not Always the Same Problem
Two code blocks that look identical but represent different **domain concepts** must NOT be merged into one shared function — they will diverge. Only merge duplication when the two blocks represent the same concept that will always change for the same reason. Ask: "If business rule A changes, does B necessarily change too?"

> **The Why:** Merging conceptually-different code that looks similar creates accidental coupling. A change to one concept then breaks the other, or you add conditional logic to the shared function to distinguish them — which is worse than the original duplication.

### 5. The Goal of Refactoring Is One Measurable Test: "Can I Add Feature X Easily?"
A refactoring has no end state unless it has a concrete acceptance criterion. Before starting: define the specific change that should become easy (e.g., "add a new ItemType without touching existing item logic"). When that change requires only adding a new file/class with no edits to existing code, the refactoring is complete.

> **The Why:** Refactoring without a stopping condition leads to endless "simplification" that never ships. A feature-addition test grounds the work in real business value and signals exactly when to stop.

---

## Golden Path Workflow

```
Step 1: Analyze  — read the code, map concepts and relationships
Step 2: Goal     — define ONE feature that should become easy to add
Step 3: Safety   — write tests covering ALL current behavior; verify green
Step 4: Refactor — rename → extract → restructure → (optionally) pattern
Step 5: Validate — add the new feature; confirm it required only additive changes
```