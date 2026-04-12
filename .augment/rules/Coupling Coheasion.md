---
type: "agent_requested"
description: "Auto-load when any of the following are detected:  File contains class definitions with 3+ external imports into a single class body Keywords: import, from ... import referencing multiple unrelated third-party libs in one file Any class method that accepts objects it then drills into deeply (e.g., obj.attr.subattr.value) File extensions: .py — any module with cross-domain data passing patterns"
---

# Rule Domain: Coupling & Cohesion
**Name:** `coupling-cohesion.md`

## Trigger
Auto-load when any of the following are detected:
- File contains class definitions with **3+ external imports** into a single class body
- Keywords: `import`, `from ... import` referencing multiple unrelated third-party libs in one file
- Any class method that accepts objects it then drills into deeply (e.g., `obj.attr.subattr.value`)
- File extensions: `.py` — any module with cross-domain data passing patterns

---

## Critical Constraints

### 1. Never Pass More Data Than a Function Actually Needs
A function that receives a large object but only uses one field is **inappropriately intimate** with that object. Pass only what is consumed. If `generate_breadcrumbs(location)` only ever accesses `location.geolocations[0]`, refactor the signature to `generate_breadcrumbs(geolocation: GeoLocation)`.

> **The Why:** Passing fat objects creates invisible coupling to the full shape of that object. Any field rename or restructure silently breaks callers that never needed the data anyway.

### 2. Never Let a Class Create Its Own Dependencies
A class that instantiates its own collaborators (e.g., `self.smtp = SMTPLib(host)` inside `__init__`) is both creator and consumer of that dependency. Separate these roles. The constructor must **receive** fully-formed dependencies, not build them.

> **The Why:** Creation and use are two different responsibilities. Mixing them makes unit testing require patching/mocking internals, and makes swapping implementations require editing the class under test.

### 3. Introduce a Protocol Class Before Adding a Third-Party Import
Before `import smtplib` (or any external lib) inside a business class, define a Protocol that specifies only the methods you actually call. The class imports the Protocol, not the library. Wire the concrete implementation at the top level.

> **The Why:** Protocols decouple the contract from the implementation via structural typing. You eliminate the import dependency without forcing the third-party class to inherit from anything — making mocks trivial and migrations painless.

### 4. Cohesion Check: The "One Sentence" Rule
Every function and class must be describable in **one sentence without the word "and"**. If the sentence requires "and", split. `update_inventory_and_notify_warehouse` is two functions masquerading as one.

> **The Why:** Low cohesion is the root cause of the most expensive refactors. A function doing two things cannot be reused safely for either thing alone.

### 5. High Coupling Warning: Count the Reasons to Change
Before committing a class, ask: how many *different external changes* could force me to edit this file? If more than one team/system can force a change (e.g., a DB schema change AND an API response shape change), the class has too many dependencies.

> **The Why:** The number of reasons a class can change equals its coupling score. Keeping it to one reason is the Single Responsibility Principle made concrete and measurable.

---

## Golden Path Example

```python
# ✅ CORRECT: Protocol decouples the class from smtplib
from typing import Protocol

class EmailServer(Protocol):
    def send_mail(self, to: str, subject: str, body: str) -> None: ...

class EmailClient:
    def __init__(self, server: EmailServer) -> None:
        self._server = server  # injected, not created

    def send_welcome(self, recipient: str) -> None:
        self._server.send_mail(recipient, "Welcome!", "Thanks for joining.")
```