from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.settings import PgSettingsRepository


class TestSetAndGet:
    def test_set_and_get(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("theme", "dark")
            conn.commit()

            result = repo.get("theme")
            assert result == "dark"

    def test_get_missing_returns_none(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            result = repo.get("nonexistent_key")
            assert result is None

    def test_get_all_returns_all_entries(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("alpha", "1")
            repo.set("beta", "2")
            repo.set("gamma", "3")
            conn.commit()

            result = repo.get_all()
            assert result == {"alpha": "1", "beta": "2", "gamma": "3"}

    def test_get_all_ordered_by_key(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("zzz", "last")
            repo.set("aaa", "first")
            repo.set("mmm", "middle")
            conn.commit()

            result = repo.get_all()
            assert list(result.keys()) == ["aaa", "mmm", "zzz"]

    def test_get_all_empty(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            result = repo.get_all()
            assert result == {}

    def test_overwrite_existing_key(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("color", "blue")
            conn.commit()

            repo.set("color", "red")
            conn.commit()

            result = repo.get("color")
            assert result == "red"

            all_settings = repo.get_all()
            # Only one entry for this key
            assert list(all_settings.keys()).count("color") == 1
