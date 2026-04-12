---
type: "agent_requested"
description: "Auto-load when any of the following are detected:  Long if/elif chains dispatching on a type string or enum (Strategy trigger) A class that calls update() / notify() on a list of other objects (Observer trigger) Keywords: Repository, CQRS, Command, Query, Handler, Observer, Strategy File extensions: .py files containing match, chained method calls, or __call__"
---

# Rule Domain: Design Patterns
**Name:** `design-patterns.md`

## Trigger
Auto-load when any of the following are detected:
- Long `if/elif` chains dispatching on a type string or enum (Strategy trigger)
- A class that calls `update()` / `notify()` on a list of other objects (Observer trigger)
- Keywords: `Repository`, `CQRS`, `Command`, `Query`, `Handler`, `Observer`, `Strategy`
- File extensions: `.py` files containing `match`, chained method calls, or `__call__`

---

## Critical Constraints

### 1. Replace `if/elif` Type Dispatch with the Strategy Pattern
Any `if isinstance(x, TypeA): ... elif isinstance(x, TypeB): ...` block that grows beyond 2 branches must become a Strategy. Define a protocol/ABC with a single `execute()` or `handle()` method; register concrete handlers in a dict keyed by type or string.

> **The Why:** Every new type added to an `elif` chain requires editing the dispatch logic — a direct violation of Open/Closed. A strategy dict means adding a type = adding a class, zero edits to existing code.

### 2. Use the Observer Pattern for Cross-Cutting Concerns — Never Direct Calls
Logging, metrics, and audit trails must **never** be called directly from domain functions. Define an `Observer` protocol with an `update(event)` method. Domain objects call `self.notify(event)` on a list of registered observers. Logging is wired at the composition root.

> **The Why:** Sprinkling logging calls through domain logic couples the domain to the logging infrastructure. Observers let you add, remove, or replace logging/metrics without touching domain code.

### 3. The Repository Pattern: One Interface Per Aggregate Root
Never query the database from service or use-case code. Define one `Repository` protocol per aggregate root (e.g., `OrderRepository`, `UserRepository`) with methods `get(id)`, `save(entity)`, `list(filter)`. The use-case receives the repository via injection.

> **The Why:** Use-case code that reaches directly into ORM sessions is untestable without a live DB. Repository protocols allow fake in-memory implementations in tests, completely eliminating DB fixtures for unit tests.

### 4. CQRS: Commands Change State, Queries Return Data — Never Both
Any function that both **modifies state** and **returns data** about that state is a CQRS violation. Write commands that return nothing (or just an ID); write queries that are pure reads with no side effects. Keep them in separate modules.

> **The Why:** Mixing reads and writes creates hidden coupling between read models and write models. This makes caching, eventual consistency, and read optimizations impossible without breaking the write path.

### 5. Never Use Singleton — Use Module-Level State or Dependency Injection
Python modules are already singletons. A `Singleton` metaclass introduces global mutable state that breaks test isolation, multi-threaded safety, and the ability to create fresh instances. Use a module-level variable or inject a shared instance at the composition root.

> **The Why:** Singletons cannot be replaced per-test, cannot be reset between tests, and cause race conditions in async/threaded code. The problem singletons solve (shared access) is solved better by injection.

---

## Golden Path Example

```python
# ✅ CORRECT: Strategy pattern — new payment type = new class, no edits elsewhere
from typing import Protocol

class PaymentStrategy(Protocol):
    def process(self, amount: float) -> None: ...

class CreditCardPayment:
    def process(self, amount: float) -> None:
        print(f"Charging card: ${amount}")

PAYMENT_STRATEGIES: dict[str, PaymentStrategy] = {
    "credit_card": CreditCardPayment(),
}

def pay(method: str, amount: float) -> None:
    PAYMENT_STRATEGIES[method].process(amount)
```