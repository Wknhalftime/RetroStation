from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.domain.system import UserSetting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_setting(conn: psycopg.Connection[dict], key: str, value: str) -> None:
    """Insert a setting directly via the repository (HTTP contract test seeding)."""
    repo = PgUserSettingRepository(conn)
    repo.upsert(UserSetting(key=key, value=value))
    conn.commit()


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/settings
# ---------------------------------------------------------------------------


class TestGetSettings:
    def test_empty_returns_empty_dict(self, client: TestClient) -> None:
        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_returns_all_seeded_settings(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_setting(db_conn, "theme", "dark")
        _seed_setting(db_conn, "language", "en")
        _seed_setting(db_conn, "volume", "75")

        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["theme"] == "dark"
        assert data["language"] == "en"
        assert data["volume"] == "75"

    def test_returns_settings_ordered_by_key(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_setting(db_conn, "zzz", "last")
        _seed_setting(db_conn, "aaa", "first")

        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200
        keys = list(resp.json().keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Tests — PUT /api/v1/settings/{key}
# ---------------------------------------------------------------------------


class TestPutSetting:
    def test_create_new_setting(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/settings/theme",
            json={"value": "light"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "theme"
        assert data["value"] == "light"

        # Verify persisted
        get_resp = client.get("/api/v1/settings")
        assert get_resp.json()["theme"] == "light"

    def test_overwrite_existing_setting(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_setting(db_conn, "theme", "dark")

        resp = client.put(
            "/api/v1/settings/theme",
            json={"value": "light"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "theme"
        assert data["value"] == "light"

        # Only one entry for the key
        get_resp = client.get("/api/v1/settings")
        result = get_resp.json()
        assert result["theme"] == "light"
        assert list(result.keys()).count("theme") == 1

    def test_create_multiple_distinct_keys(self, client: TestClient) -> None:
        client.put("/api/v1/settings/alpha", json={"value": "1"})
        client.put("/api/v1/settings/beta", json={"value": "2"})

        get_resp = client.get("/api/v1/settings")
        result = get_resp.json()
        assert result["alpha"] == "1"
        assert result["beta"] == "2"
