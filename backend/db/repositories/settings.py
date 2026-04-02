from __future__ import annotations

from typing import Any

import psycopg

from backend.repositories.settings import SettingsRepository


class PgSettingsRepository(SettingsRepository):
    """PostgreSQL-backed implementation of :class:`SettingsRepository`.

    Operates against the ``user_settings`` table, which has the schema::

        user_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ)
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def get(self, key: str) -> str | None:
        """Return the value for *key*, or ``None`` if not found.

        Args:
            key: Settings key to look up.

        Returns:
            The stored value string, or ``None``.
        """
        row = self._conn.execute(
            "SELECT value FROM user_settings WHERE key = %s",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set(self, key: str, value: str) -> None:
        """Insert or update *key* with *value*.

        Uses ``ON CONFLICT (key) DO UPDATE`` so existing keys are overwritten.

        Args:
            key: Settings key.
            value: New value to store.
        """
        self._conn.execute(
            """
            INSERT INTO user_settings (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = now()
            """,
            (key, value),
        )

    def get_all(self) -> dict[str, str]:
        """Return all settings as a ``key → value`` mapping, ordered by key.

        Returns:
            Dictionary of all stored settings.
        """
        rows = self._conn.execute(
            "SELECT key, value FROM user_settings ORDER BY key"
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}
