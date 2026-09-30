"""Router: DELETE /library/missing-files bounds the ids a body may name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from backend.routers.library.missing import MAX_DELETE_IDS

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

URL = "/api/v1/library/missing-files"


def test_more_ids_than_the_bound_is_422(client: TestClient) -> None:
    ids = [str(uuid4()) for _ in range(MAX_DELETE_IDS + 1)]

    response = client.request("DELETE", URL, json={"ids": ids})

    assert response.status_code == 422


def test_ids_up_to_the_bound_are_accepted(client: TestClient) -> None:
    ids = [str(uuid4()) for _ in range(MAX_DELETE_IDS)]

    response = client.request("DELETE", URL, json={"ids": ids})

    assert response.status_code == 200
    assert response.json() == {"deleted": 0, "matches_released": 0, "skipped": MAX_DELETE_IDS}
