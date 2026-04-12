---
type: "agent_requested"
description: "Trigger Auto-load when any of the following are detected:  Keywords: openai, anthropic, llm, agent, prompt, completion, pydantic_ai, langchain, tool_call A single function that constructs a prompt AND calls an LLM AND parses the response AND writes to a database File contains response.choices[0].message.content or equivalent SDK patterns Task description involves:"
---

# Rule Domain: AI Agent & LLM Integration
**Name:** `ai-agent-integration.md`

## Trigger
Auto-load when any of the following are detected:
- Keywords: `openai`, `anthropic`, `llm`, `agent`, `prompt`, `completion`, `pydantic_ai`, `langchain`, `tool_call`
- A single function that constructs a prompt AND calls an LLM AND parses the response AND writes to a database
- File contains `response.choices[0].message.content` or equivalent SDK patterns
- Task description involves: "AI agent", "multi-step pipeline", "tool use", "LLM workflow"

---

## Critical Constraints

### 1. Each Agent Does Exactly One Thing (Chain of Responsibility)
An LLM pipeline must be decomposed into a **chain of single-responsibility agents**. `choose_destination()`, `plan_flight()`, `recommend_hotel()` are three agents — never one. Each agent receives the same typed `Context` object and enriches it. The orchestrator calls them sequentially; no agent calls another agent directly.

> **The Why:** A monolithic "do everything" prompt exceeds context window limits, produces worse results, and is impossible to debug. Single-responsibility agents can be tested, swapped, and enabled/disabled independently. Smaller context = better LLM output.

### 2. Use the Observer Pattern for LLM Observability — Never Inline Logging
Never add `print(f"Prompt: {prompt}")` inside an agent function. Define an `AgentObserver` protocol with a `on_call(prompt, response, duration)` method. The orchestrator registers observers (logging, metrics, tracing). Agent functions call `self.notify(event)` — they have no knowledge of what observes them.

> **The Why:** LLM calls are expensive and opaque. Observability is a cross-cutting concern. Inline logging couples agent logic to a specific logging strategy, making it impossible to swap structured logging, send to Datadog, or silence in tests without editing every agent.

### 3. Structured Outputs: Prompt for JSON, Validate with Pydantic, Never Parse with Regex
When an LLM must return structured data, the system prompt must specify exact JSON schema. The response must be parsed with `model.model_validate_json(response)`. Never use `re.findall`, string slicing, or `eval()` to extract data from an LLM response. Add a retry step for JSON parse failures.

> **The Why:** LLM output is probabilistic text. Regex and string parsing fail silently on minor format variations. Pydantic validation raises immediately with a clear error and enables the retry pattern to handle transient malformation.

### 4. Context Objects Are Immutable Across the Chain
The shared `Context` dataclass passed between agents must be `frozen=True` (or a `TypedDict`). Agents return an **updated copy** (`dataclasses.replace(ctx, destination="Paris")`), never mutate the shared context in place. The orchestrator assembles the final state from successive return values.

> **The Why:** Mutable shared context between agents creates ordering-dependent bugs that are nearly impossible to reproduce. Immutable context + returned copies makes every agent's transformation explicit and auditable.

### 5. Never Hardcode System Prompts Inline — Treat Them as Configuration
System prompts must live in dedicated `.txt` or `.md` files (or a prompt registry), not as multiline f-strings inside function bodies. The agent receives the prompt as a parameter at construction time. This enables A/B testing of prompts, version control of prompt changes, and separation of prompt engineering from code logic.

> **The Why:** A system prompt change should not require a code deployment. Prompts are configuration, not code. Mixing them forces developers to touch Python files for what is fundamentally a product/content change.

---

## Golden Path Example

```python
# ✅ CORRECT: Single-responsibility agent, typed context, no inline logging
from dataclasses import dataclass, replace
from typing import Protocol

@dataclass(frozen=True)
class TripContext:
    origin: str
    destination: str | None = None

class AgentObserver(Protocol):
    def on_call(self, prompt: str, response: str, duration_ms: int) -> None: ...

def choose_destination(ctx: TripContext, prompt: str, observer: AgentObserver) -> TripContext:
    response = call_llm(prompt.format(origin=ctx.origin))  # isolated LLM call
    observer.on_call(prompt, response, duration_ms=42)
    return replace(ctx, destination=response.strip())
```