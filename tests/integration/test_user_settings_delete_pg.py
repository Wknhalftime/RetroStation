"""``UserSettingRepository.delete`` on PostgreSQL (PR G1, Task 1; traceability C: T1.9).

Requirements: H6 / S27 (the repository ABC's new ``delete``, implemented by the Pg adapter
exactly as by the fake); D26 (removing the sign-off clip deletes its setting).
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.domain.system import UserSetting


def test_delete_removes_the_row_and_a_missing_key_is_fine(migrated_db: str) -> None:
    # T1.9: the row goes, the others stay, and deleting a key that is not there is no error.
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgUserSettingRepository(conn)
        repo.upsert(UserSetting(key="stream_sign_off", value="{}"))
        repo.upsert(UserSetting(key="stream_max_sessions", value="3"))
        conn.commit()

        repo.delete("stream_sign_off")
        repo.delete("never-set")
        conn.commit()

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgUserSettingRepository(conn)
        assert repo.get("stream_sign_off") is None
        assert [s.key for s in repo.list_all()] == ["stream_max_sessions"]
