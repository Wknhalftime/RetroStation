from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.domain.system import UserSetting


class TestUpsertAndGet:
    def test_upsert_and_get(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            repo.upsert(UserSetting(key="theme", value="dark"))
            conn.commit()

            result = repo.get("theme")
            assert result is not None
            assert result.key == "theme"
            assert result.value == "dark"
            assert result.updated_at is not None

    def test_get_missing_returns_none(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            result = repo.get("nonexistent_key")
            assert result is None

    def test_upsert_returns_entity_with_db_timestamp(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            returned = repo.upsert(UserSetting(key="x", value="y"))
            assert returned.key == "x"
            assert returned.value == "y"
            assert returned.updated_at is not None

    def test_list_all_returns_all_entries(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            repo.upsert(UserSetting(key="alpha", value="1"))
            repo.upsert(UserSetting(key="beta", value="2"))
            repo.upsert(UserSetting(key="gamma", value="3"))
            conn.commit()

            result = repo.list_all()
            assert [(s.key, s.value) for s in result] == [
                ("alpha", "1"), ("beta", "2"), ("gamma", "3"),
            ]

    def test_list_all_ordered_by_key(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            repo.upsert(UserSetting(key="zzz", value="last"))
            repo.upsert(UserSetting(key="aaa", value="first"))
            repo.upsert(UserSetting(key="mmm", value="middle"))
            conn.commit()

            result = repo.list_all()
            assert [s.key for s in result] == ["aaa", "mmm", "zzz"]

    def test_list_all_empty(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            result = repo.list_all()
            assert result == []

    def test_overwrite_existing_key(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            repo.upsert(UserSetting(key="color", value="blue"))
            conn.commit()

            repo.upsert(UserSetting(key="color", value="red"))
            conn.commit()

            result = repo.get("color")
            assert result is not None
            assert result.value == "red"

            all_settings = repo.list_all()
            assert [s.key for s in all_settings].count("color") == 1

    def test_empty_string_value_is_stored_and_retrieved(self, migrated_db: str) -> None:
        """Empty string is a valid value; must not be confused with None/missing."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgUserSettingRepository(conn)
            repo.upsert(UserSetting(key="empty_key", value=""))
            conn.commit()

            result = repo.get("empty_key")
            assert result is not None
            assert result.value == ""
