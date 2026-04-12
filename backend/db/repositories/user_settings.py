from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.system import UserSetting
from backend.repositories.user_settings import UserSettingRepository


class PgUserSettingRepository(UserSettingRepository):
    """PostgreSQL-backed implementation of :class:`UserSettingRepository`.

    Operates against the ``user_settings`` table, which has the schema::

        user_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ)
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def get(self, key: str) -> UserSetting | None:
        """Return the :class:`UserSetting` for *key*, or ``None`` if not found.

        Args:
            key: Settings key to look up.

        Returns:
            The stored :class:`UserSetting`, or ``None``.
        """
        row = self._conn.execute(
            "SELECT key, value, updated_at FROM user_settings WHERE key = %s",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return UserSetting(
            key=str(row["key"]),
            value=str(row["value"]),
            updated_at=row["updated_at"],
        )

    def upsert(self, setting: UserSetting) -> UserSetting:
        """Insert or update *setting*, returning the persisted entity.

        Uses ``ON CONFLICT (key) DO UPDATE`` so existing keys are overwritten.
        The returned :class:`UserSetting` carries the database-generated ``updated_at``.

        Args:
            setting: The :class:`UserSetting` to persist.

        Returns:
            Persisted :class:`UserSetting` with ``updated_at`` from the database.
        """
        row = self._conn.execute(
            """
            INSERT INTO user_settings (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = now()
            RETURNING key, value, updated_at
            """,
            (setting.key, setting.value),
        ).fetchone()
        assert row is not None  # INSERT … RETURNING always returns a row
        return UserSetting(
            key=str(row["key"]),
            value=str(row["value"]),
            updated_at=row["updated_at"],
        )

    def list_all(self) -> list[UserSetting]:
        """Return all settings as a list of :class:`UserSetting`, ordered by key.

        Returns:
            All stored settings, sorted alphabetically by key.
        """
        rows = self._conn.execute(
            "SELECT key, value, updated_at FROM user_settings ORDER BY key"
        ).fetchall()
        return [
            UserSetting(
                key=str(row["key"]),
                value=str(row["value"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

