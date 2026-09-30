"""Uvicorn entry point that forces SelectorEventLoop on Windows.

Uvicorn's default ``asyncio`` loop factory explicitly returns ProactorEventLoop
on Windows, which psycopg's async driver does not support.  Setting the event
loop *policy* is not enough because uvicorn passes a ``loop_factory`` to
``asyncio.run()``, bypassing the policy entirely.

Fix: pass ``loop="none"`` so uvicorn defers to the policy, then set the policy
to WindowsSelectorEventLoopPolicy. The host and port come from the settings (D24).
"""

import asyncio
import sys

import uvicorn

from backend.config import get_settings


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=str(settings.server_host),
        port=settings.server_port,
        loop="none",
    )


if __name__ == "__main__":
    main()
