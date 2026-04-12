from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

# Level strings emitted by structlog.processors.add_log_level → DB LogLevel values.
# Kept as plain strings to avoid importing domain enums (which pull in psycopg etc.)
# into every process that calls configure_logging.
_LEVEL_MAP: dict[str, str] = {
    "debug":    "DEBUG",
    "info":     "INFO",
    "warning":  "WARNING",
    "warn":     "WARNING",
    "error":    "ERROR",
    "critical": "ERROR",
}

_RESERVED_KEYS = {"event", "level", "timestamp", "category", "_record", "_from_structlog"}


class DbLogProcessor:
    """Structlog processor that persists every log event to the ``system_logs`` table.

    Opens a single autocommit psycopg connection lazily on first use.  DB errors
    are silently swallowed (printed to stderr) so that a database outage never
    crashes the logging pipeline.

    The processor runs **after** ``add_log_level`` and ``TimeStamper`` in the
    chain so that ``event_dict`` already contains ``"level"`` and ``"timestamp"``.
    It returns ``event_dict`` unchanged so the subsequent console/JSON renderer
    still produces terminal output as before.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._conn: Any = None  # psycopg.Connection, imported lazily

    def _get_conn(self) -> Any:
        """Return the open autocommit connection, opening it on first call."""
        if self._conn is None or self._conn.closed:
            import psycopg  # noqa: PLC0415 – lazy import to keep the module light
            from psycopg.rows import dict_row  # noqa: PLC0415

            self._conn = psycopg.connect(
                self._database_url,
                row_factory=dict_row,
                autocommit=True,
            )
            self._conn.execute("SET search_path TO public, pg_catalog")
        return self._conn

    def __call__(
        self,
        logger: Any,
        method: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            conn = self._get_conn()

            level_str = _LEVEL_MAP.get(str(event_dict.get("level", "info")).lower(), "INFO")
            category  = str(event_dict.get("category", "system"))
            message   = str(event_dict.get("event", ""))
            details   = {k: v for k, v in event_dict.items() if k not in _RESERVED_KEYS} or None

            conn.execute(
                """INSERT INTO system_logs
                       (id, trace_id, category, level, message, details, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(uuid4()),
                    event_dict.get("trace_id"),
                    category,
                    level_str,
                    message,
                    json.dumps(details) if details is not None else None,
                    datetime.now(UTC),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[DbLogProcessor] failed to write to system_logs: {exc}", file=sys.stderr)

        return event_dict


def configure_logging(log_level: str = "INFO", database_url: str | None = None) -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        log_level: Minimum log level (``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
            ``"ERROR"``).  Case-insensitive.
        database_url: When provided, a :class:`DbLogProcessor` is added to the
            processor chain so that every log event is also persisted to the
            ``system_logs`` PostgreSQL table.  Pass ``None`` (the default) to
            keep pure-stdout logging (e.g. in tests).
    """
    # Must reconfigure before any other logging to prevent cp1252 Unicode crashes on Windows
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if database_url is not None:
        processors.append(DbLogProcessor(database_url))

    processors.append(
        structlog.dev.ConsoleRenderer() if log_level == "DEBUG"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
