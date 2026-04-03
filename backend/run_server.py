"""Uvicorn entry point that forces SelectorEventLoop on Windows.

Uvicorn's default ``asyncio`` loop factory explicitly returns ProactorEventLoop
on Windows, which psycopg's async driver does not support.  Setting the event
loop *policy* is not enough because uvicorn passes a ``loop_factory`` to
``asyncio.run()``, bypassing the policy entirely.

Fix: pass ``loop="none"`` so uvicorn defers to the policy, then set the policy
to WindowsSelectorEventLoopPolicy.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, loop="none")
