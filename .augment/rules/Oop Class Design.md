---
type: "agent_requested"
description: "uto-load when any of the following are detected:  File contains class definitions with __init__, @dataclass, or @property Keywords: class, inherit, super().__init__, @abstractmethod, dataclass A class inherits from another non-abstract class (composition vs inheritance decision point) Primitive obsession: function signatures with 4+ raw str, int, float parameters representing domain concepts"
---

# Rule Domain: OOP & Class Design
**Name:** `oop-class-design.md`

## Trigger
Auto-load when any of the following are detected:
- File contains class definitions with `__init__`, `@dataclass`, or `@property`
- Keywords: `class`, `inherit`, `super().__init__`, `@abstractmethod`, `dataclass`
- A class inherits from another non-abstract class (composition vs inheritance decision point)
- Primitive obsession: function signatures with 4+ raw `str`, `int`, `float` parameters representing domain concepts

---

## Critical Constraints

### 1. Prefer Composition Over Inheritance — Always
Before using `class Child(Parent)`, ask: does `Child` need **all** of `Parent`'s interface, or just **some** of its behavior? If the answer is "some", use composition (hold a `Parent` instance as an attribute). Only use inheritance when `Child` is genuinely a specific *kind* of `Parent` in the domain model and must satisfy the Liskov Substitution Principle.

> **The Why:** Inheritance couples the child to every attribute and method of the parent, including ones it doesn't use. Composition exposes only the interface you need, keeping coupling minimal and making the collaborator swappable.

### 2. Use `@dataclass` for Data Containers, Classes for Behavior
A class that is only a named grouping of fields (no methods beyond `__init__`, no invariants to enforce) must be a `@dataclass`. A class that has behavior, enforces invariants, or performs transformations is a full class. Never mix: a `@dataclass` with 10 methods is a smell requiring a split.

> **The Why:** `@dataclass` communicates "this is a record / value object" to the reader immediately. Mixing data and behavior in a dataclass destroys that semantic signal and leads to anemic domain models with business logic scattered in utilities.

### 3. Value Objects: Wrap Primitives That Have Domain Meaning
Any primitive (`str`, `int`, `float`) that carries a domain constraint (e.g., "SKU must be uppercase alphanumeric", "quantity must be positive") must become a Value Object. Validate in `__post_init__` or `__init__` and make instances immutable. Never pass raw strings where a `SKU` type is intended.

> **The Why:** Raw primitives bypass domain invariants. `order.sku = ""` is valid Python but invalid business logic. A `SKU` value object raises on construction, making invalid state unrepresentable.

### 4. Never Use Mutable Default Arguments or Global State in Class Bodies
`def __init__(self, items=[])` is a Python gotcha that creates a shared list across all instances. Always use `None` as default and initialize mutable defaults inside the body. Class-level mutable attributes (`class Foo: cache = {}`) are global state and must be replaced with instance attributes or injected stores.

> **The Why:** Shared mutable default arguments cause the most confusing bugs in Python because they appear to be local but are not. They violate the principle that each object instance owns its own state.

### 5. Functions Are Preferable to Classes When There Is No State to Maintain
A class with only `__init__` and one method (effectively a callable object) should be a function. A class with only `@staticmethod` methods is a namespace, not an object. Reserve classes for cases where the object genuinely maintains state between calls or represents a domain entity.

> **The Why:** Unnecessary classes add indirection without adding expressiveness. They force the reader to understand instantiation, lifetime, and identity where none of those things matter. A function is the simplest possible unit of reuse.

---

## Golden Path Example

```python
# ✅ CORRECT: Value Object enforces invariants at construction
from dataclasses import dataclass

@dataclass(frozen=True)
class SKU:
    value: str

    def __post_init__(self):
        if not self.value.isalnum() or not self.value.isupper():
            raise ValueError(f"Invalid SKU: {self.value!r}")

# Usage — invalid state cannot be created silently
sku = SKU("WIDGET42")   # ✅
sku = SKU("widget 42")  # ✅ raises ValueError immediately
```