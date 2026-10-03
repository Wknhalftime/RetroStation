"""The generic settings route validates through the settings rules (PR G1, Task 1;
traceability B: T1.6-T1.8).

Requirements: D27 (a listener limit that is not a whole number >= 1 is refused on save);
PG2 (D27 on every save path, the generic ``PUT /api/v1/settings/{key}`` included;
``stream_sign_off`` cannot be put directly); "every 422 that G raises uses FastAPI's list
shape" (plan, Global Constraints).
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.db.repositories.user_settings import PgUserSettingRepository

pytestmark = pytest.mark.integration

LIMIT = "stream_max_sessions"
SIGN_OFF = "stream_sign_off"


def stored(conn: psycopg.Connection[dict], key: str) -> str | None:
    found = PgUserSettingRepository(conn).get(key)
    return None if found is None else found.value


def assert_list_shaped_422_naming(response_json: object, key: str) -> None:
    assert isinstance(response_json, dict)
    detail = response_json["detail"]
    assert isinstance(detail, list) and detail
    assert all({"loc", "msg", "type"} <= set(entry) for entry in detail)
    assert any(key in entry["msg"] for entry in detail)


@pytest.mark.parametrize("value", ["0", "abc"])
def test_putting_a_bad_listener_limit_is_422_naming_the_setting(
    client: TestClient, db_conn: psycopg.Connection[dict], value: str
) -> None:
    # T1.6 (D27, PG2): refused, in FastAPI's list shape, and nothing is stored.
    response = client.put(f"/api/v1/settings/{LIMIT}", json={"value": value})
    assert response.status_code == 422
    assert_list_shaped_422_naming(response.json(), LIMIT)
    assert stored(db_conn, LIMIT) is None


def test_putting_a_good_listener_limit_is_stored(
    client: TestClient, db_conn: psycopg.Connection[dict]
) -> None:
    # T1.7 (D27; guard on master): a whole number >= 1 is still stored as before.
    response = client.put(f"/api/v1/settings/{LIMIT}", json={"value": "5"})
    assert response.status_code == 200
    assert response.json() == {"key": LIMIT, "value": "5"}
    assert stored(db_conn, LIMIT) == "5"


def test_the_sign_off_setting_cannot_be_put_directly(
    client: TestClient, db_conn: psycopg.Connection[dict]
) -> None:
    # T1.8 (PG2): only the clip upload writes stream_sign_off.
    value = '{"file_name": "0123456789abcdef.mp3", "format": "mp3", "span_ms": 5000}'
    response = client.put(f"/api/v1/settings/{SIGN_OFF}", json={"value": value})
    assert response.status_code == 422
    assert_list_shaped_422_naming(response.json(), SIGN_OFF)
    assert stored(db_conn, SIGN_OFF) is None
