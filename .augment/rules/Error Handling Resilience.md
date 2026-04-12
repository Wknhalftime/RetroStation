---
type: "agent_requested"
description: "Auto-load when any of the following are detected:  Keywords: try, except, raise, Exception, retry, HTTPError, timeout A try/except block that catches an exception and then calls a different function (control-flow abuse) Third-party API calls without explicit retry/fallback logic Custom exception class definitions"
---

# Rule Domain: Error Handling & Resilience
**Name:** `error-handling-resilience.md`

## Trigger
Auto-load when any of the following are detected:
- Keywords: `try`, `except`, `raise`, `Exception`, `retry`, `HTTPError`, `timeout`
- A `try/except` block that catches an exception and then calls a *different* function (control-flow abuse)
- Third-party API calls without explicit retry/fallback logic
- Custom exception class definitions

---

## Critical Constraints

### 1. Never Use `try/except` for Control Flow
`try/except` that catches an exception in order to branch to alternative logic (e.g., `except TimeoutError: return fetch_backup()`) is not error handling — it is flow control wearing a disguise. Use conditional checks (`if available`, `if exists`) instead. Reserve `try/except` for genuinely *exceptional* runtime conditions that cannot be predicted.

> **The Why:** Exceptions used for flow control are expensive (stack unwinding), obscure intent (the happy path is invisible), and create silent bugs when new exception subtypes are added that accidentally match the catch clause.

### 2. Handle Low-Level Exceptions at Their Layer; Raise Domain Exceptions Upward
A SQLite `OperationalError` must be caught at the database access layer and re-raised as a domain-level exception (e.g., `BlogNotFoundError`). API routes must catch domain exceptions only. No route should ever handle `sqlite3.OperationalError` directly.

> **The Why:** If API routes know about SQLite exceptions, swapping the database requires auditing every route. Layered exception translation keeps each layer ignorant of implementation details below it.

### 3. Define Custom Exception Hierarchies Per Domain, Not Per Function
Create a base exception for the domain (e.g., `class DomainError(Exception): pass`) and subclass specific errors from it (`NotFoundError`, `NotAuthorizedError`, `ValidationError`). Never `raise Exception("some message")` — naked exceptions lose type-based catch precision.

> **The Why:** Type-based exception handling (`except NotFoundError`) is the only way to catch specific conditions without accidentally silencing unrelated errors. `except Exception` is almost always a bug.

### 4. Apply the Retry Pattern Only to Idempotent, Transient-Failure Operations
Retry logic (exponential backoff, max attempts) is valid only when: (a) the failure is known to be temporary (network blip, rate limit), and (b) repeating the operation has no side effects. Never retry database writes without idempotency keys. Never retry operations that depend on user input (invalid input won't fix itself).

> **The Why:** Blindly retrying non-idempotent operations (e.g., "create order") can cause duplicate records. Retrying permanent failures (wrong API key) wastes time and creates retry storms that amplify the original outage.

### 5. Use Decorators for Cross-Cutting Concerns Like Logging and Retry — Never Inline
Logging exceptions and retry logic must be implemented as reusable decorators, not copy-pasted `try/except` blocks in every function. A decorator applied at the definition site is explicit, auditable, and consistent.

> **The Why:** Inline retry and logging code duplicates error-handling policy across dozens of functions. When policy changes (e.g., max retries goes from 3 to 5), one decorator edit propagates everywhere; inline code requires a grep-and-replace across the codebase.

---

## Golden Path Example

```python
# ✅ CORRECT: Custom exceptions + decorator retry + layered translation
import functools, time

def retry(times: int = 3, delay: float = 1.0):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(times):
                try:
                    return fn(*args, **kwargs)
                except TransientAPIError:
                    if attempt == times - 1: raise
                    time.sleep(delay * (2 ** attempt))
        return wrapper
    return decorator

class BlogNotFoundError(DomainError): pass

@retry(times=3, delay=0.5)
def fetch_blog(blog_id: int) -> Blog:
    try:
        return db.query(Blog).filter_by(id=blog_id).one()
    except NoResultFound:
        raise BlogNotFoundError(blog_id)
```