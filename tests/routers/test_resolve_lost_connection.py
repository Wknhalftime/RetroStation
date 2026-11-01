"""Resolve and Unmatch answer 503 when the database connection is lost (artist model phase 1a,
C75; the diff review of PR #214), as the other sync routes do: the decision's adapters and its
commit raise the domain's ``StorageUnavailableError``, never a psycopg error, and nothing is
queued."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.domain.matching import MatchingStorageError


def _lost(*_args: Any) -> None:
    raise MatchingStorageError("the database connection was lost")


def test_resolve_answers_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    enqueue = MagicMock()
    monkeypatch.setattr("backend.routers.matching.identity_matching_task", enqueue)
    monkeypatch.setattr("backend.routers.matching.match_by_hand", _lost)

    response = client.post(
        f"/api/v1/matching/artists/{uuid4()}/resolve",
        json={"match_status": "manual_matched", "target_artist_id": "an-mbid"},
    )

    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})
    enqueue.assert_not_called()


def test_unmatch_answers_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.routers.matching.unmatch", _lost)

    response = client.post(f"/api/v1/matching/artists/{uuid4()}/unmatch")

    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})
