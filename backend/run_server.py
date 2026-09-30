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

_GRACEFUL_SHUTDOWN_S = 3  # then open streams are cancelled and the lifespan shuts down


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=str(settings.server_host),
        port=settings.server_port,
        loop="none",
        # An open /listen stream never ends by itself; without a bound, shutdown waits on it
        # forever, and a second Ctrl+C skips the lifespan shutdown that stops the engines.
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
    )


if __name__ == "__main__":
    main()
