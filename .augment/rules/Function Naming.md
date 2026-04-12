---
type: "agent_requested"
description: "Rules for Naming Function And Classes"
---

## Rules for Naming Functions and Classes

### Function Naming

- **Use a verb** — function names must describe an action (e.g., `calculate_total_price`, `send_invoice`, `fetch_user_data`)
- **Arguments should be nouns** — precise and unambiguous (e.g., `item_prices: list[int]`, not `items` or `data`)
- **Use snake_case** in Python; camelCase in JavaScript/TypeScript — follow the language convention consistently
- **Be descriptive, not generic** — avoid names like `process`, `handle`, `do_stuff`, `run`, `execute` without context
- **Never encode the return type in the name** — use type annotations instead (avoid `get_price_as_integer`; prefer `get_price() -> int`)
- **Use consistent vocabulary** — if something is an `order`, never call it a `request` or `transaction` elsewhere in the same codebase
- **No abbreviations** — avoid `lib`, `mgr`, `proc`, `cfg`, `l`, `obj` unless universally understood (e.g., `id`, `url`)
- **No typos or grammar errors** in names — these become permanent and painful to work around
- **Prefix private/internal functions with `_`** to signal they are not part of the public API

### Class Naming

- **Use a noun** that describes what the class *represents*, not what it *does* (e.g., `OrderProcessor`, `InvoiceSummary`, not `DoOrderThings`)
- **Avoid vague class names** like `Manager`, `Handler`, `Helper`, `Util`, `Base` — be specific about what it manages or handles
- **If a class has only one method and no meaningful state**, it is a function in disguise — refactor it into a standalone function
- **If a class has only static methods**, it is a module in disguise — refactor it into a module/file of functions

---

## Warning Signs: A Function Is Doing Too Much

- **The name contains "and"** — e.g., `collect_and_summarize_invoices` signals two responsibilities; split into `collect_invoices` and `summarize_invoices`
- **More than 4 arguments** — consider grouping related parameters into a dataclass or config object
- **You cannot describe the function without saying "and"** — it is doing too much
- **It mixes abstraction levels** — e.g., combining raw I/O, business logic, and formatting in one function
- **Multiple unrelated side effects** — e.g., saving to a database *and* sending an email *and* logging
- **It is hard to unit test in isolation** — usually means it has hidden dependencies or too many responsibilities

---

## Warning Signs: A Class Is Doing Too Much

- **More than 5–7 instance variables** — likely needs to be split into two or more focused classes
- **The class handles unrelated concerns** — e.g., an `Order` class that also processes payments or manages customer addresses should be split
- **The class name is generic** — `Manager`, `Processor`, `Handler` without a clear domain noun is a red flag
- **You can extract a group of methods and their data into a new class** that makes independent sense — do it
- **Deep or tangled inheritance** — prefer composition over inheritance; avoid mixins where composition is cleaner
- **God class** — if a single class seems to "know about everything" in the system, it violates single responsibility

---

## General Principles to Always Apply

| Principle | Rule |
|---|---|
| **Single Responsibility** | Each function and class should have one reason to change |
| **High Cohesion** | Everything inside a unit should belong together and serve one purpose |
| **Low Coupling** | Minimize dependencies between unrelated parts of the code |
| **Functions over Classes** | Prefer functions and modules when there is no persistent state and no need for multiple instances |
| **Data vs. Behavior** | Try to make classes either data-focused (fields + a few methods) or behavior-focused — not both heavily |
| **Type Annotations** | Always annotate arguments and return types — they act as embedded documentation |

---

## Quick Reference: Good vs. Bad Examples

```python
# ❌ BAD — vague name, no verb, unclear argument
def compute(items, x):
    ...

# ✅ GOOD — clear verb, descriptive noun arguments, type annotations
def calculate_total_price(item_prices: list[int], discount: int) -> int:
    ...

# ❌ BAD — function doing too much (name contains "and")
def collect_and_summarize_invoices():
    ...

# ✅ GOOD — split into focused functions
def collect_invoices() -> list[Invoice]:
    ...

def summarize_invoices(invoices: list[Invoice]) -> InvoiceSummary:
    ...

# ❌ BAD — class masquerading as a function (one method, no state)
class DataLoader:
    def __init__(self, file_path: Path): self.file_path = file_path
    def load(self): ...

# ✅ GOOD — just use a function
def load_data(file_path: Path) -> Data:
    ...

# ❌ BAD — god class with unrelated responsibilities
class Order:
    def add_item(self): ...
    def compute_total(self): ...
    def process_payment(self): ...   # payment logic does not belong here
    def send_confirmation_email(self): ...  # email logic does not belong here

# ✅ GOOD — separated by responsibility
class Order:
    def add_item(self): ...
    def compute_total(self): ...

class PaymentProcessor:
    def process(self, order: Order): ...

class NotificationService:
    def send_confirmation(self, order: Order): ...
```