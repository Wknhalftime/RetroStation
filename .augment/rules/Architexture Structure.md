---
type: "agent_requested"
description: "Auto-load when any of the following are detected:  File is inside a routers/, api/, endpoints/, or views/ directory Keywords: FastAPI, Flask, Django, HTTPException, sqlalchemy, Session inside a file that also contains business logic keywords (calculate, validate, process) A domain/service function imports directly from fastapi, flask, or an ORM File extensions: .py in projects with both an api/ and domain/ or services/ directory"
---

# Rule Domain: Architecture & Project Structure
**Name:** `architecture-structure.md`

## Trigger
Auto-load when any of the following are detected:
- File is inside a `routers/`, `api/`, `endpoints/`, or `views/` directory
- Keywords: `FastAPI`, `Flask`, `Django`, `HTTPException`, `sqlalchemy`, `Session` inside a file that also contains business logic keywords (`calculate`, `validate`, `process`)
- A domain/service function imports directly from `fastapi`, `flask`, or an ORM
- File extensions: `.py` in projects with both an `api/` and `domain/` or `services/` directory

---

## Critical Constraints

### 1. Domain Functions Must Have Zero Framework Imports
Any function inside `domain/` or `services/` that contains `from fastapi import`, `from flask import`, `from sqlalchemy import`, or `raise HTTPException` is an architecture violation. Domain functions raise **domain exceptions** only; translation to HTTP codes happens in the adapter layer.

> **The Why:** Framework imports inside domain code mean you cannot run or test the domain without the full framework stack. Ports & Adapters guarantees that your core business logic runs in pure Python with no external servers.

### 2. Ports Live in the Domain; Adapters Live Outside It
Define your `Protocol` interfaces (ports) inside the `domain/ports.py` file. Concrete implementations (SQLAlchemy adapters, HTTP clients, email senders) live in `adapters/`. The domain never imports from `adapters/`. The dependency arrow points inward.

> **The Why:** Inverting the dependency direction means the domain is the stable core. Swapping SQLAlchemy for a different ORM, or REST for gRPC, requires creating a new adapter only — zero changes to domain logic.

### 3. FastAPI Endpoints Do Exactly Three Things
An endpoint function is allowed to: (1) parse/validate input via Pydantic, (2) call a single domain use-case, (3) map domain exceptions to HTTP responses. Any business logic (calculations, conditionals on domain state, direct DB calls) in an endpoint is a violation.

> **The Why:** Endpoints that contain business logic cannot be reused across CLI tools, background workers, or tests without spinning up the HTTP server. Three-step endpoints are trivially testable and swappable.

### 4. Project Folder Structure Must Mirror Architecture Layers
```
project/
  domain/          # Pure Python. Zero framework imports.
    models.py
    ports.py       # Protocol definitions
    use_cases.py
  adapters/        # Framework-specific implementations
    sqlalchemy_repo.py
    email_adapter.py
  api/             # FastAPI/Flask routers. Translation only.
    routers.py
  main.py          # Composition root. Wire everything here.
```
Any deviation from this structure (e.g., a `utils.py` at the top level that mixes DB helpers with string formatting) is a smell requiring immediate separation.

> **The Why:** When folder structure mirrors architectural boundaries, new developers and AI coding assistants can determine where code belongs without reading the entire codebase.

### 5. The Composition Root is the Only Place to Wire Dependencies
`main.py` (or `app.py`) is the single file where concrete adapters are instantiated and injected into use-cases and routers. No other file calls constructors for injectable collaborators. This is the *only* place `from adapters import` is allowed in combination with `from domain import`.

> **The Why:** A single wiring file means changing any concrete implementation (DB, email provider) requires editing exactly one file. It also makes the system's full dependency graph visible in one place.

---

## Golden Path Example

```python
# ✅ domain/use_cases.py — zero framework imports
from domain.ports import InventoryPort
from domain.models import Order, OrderPlaced
from domain.exceptions import OutOfStockError

def place_order(inventory: InventoryPort, sku: str, qty: int) -> OrderPlaced:
    if not inventory.exists(sku):
        raise UnknownSKUError(sku)
    if inventory.get_stock(sku) < qty:
        raise OutOfStockError(sku, qty)
    remaining = inventory.reserve(sku, qty)
    return OrderPlaced(sku=sku, quantity=qty, remaining_stock=remaining)
```