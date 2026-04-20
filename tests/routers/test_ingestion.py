"""Router tests for the CSV ingestion endpoint.

Key invariant: the ``task_id`` returned in the response body must be
identical to the value passed to ``ingestion_task(...)``. This is the
"single source of truth" rule — the client and the ``progress_tracking``
table must agree on one ID.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

CSV_PAYLOAD = (
    b"Station,Played,Artist,Title\r\n"
    b"KAZR,2005-03-02 00:01:00,Artist_A,Title_A\r\n"
)


def test_upload_playlist_returns_task_id_matching_enqueue(
    client: TestClient,
) -> None:
    with patch("backend.routers.ingestion.ingestion_task") as enqueue:
        resp = client.post(
            "/api/v1/ingestion/playlists",
            files={"file": ("x.csv", CSV_PAYLOAD, "text/csv")},
            data={"station_id": "00000000-0000-0000-0000-000000000001"},
        )

    assert resp.status_code == 202
    body = resp.json()
    returned_id = body["task_id"]
    assert returned_id and isinstance(returned_id, str)

    # Single source of truth: the ID returned to the client IS the ID the
    # worker receives and will write into progress_tracking.
    assert enqueue.call_count == 1
    (_bytes, _name, _station, enqueued_id) = enqueue.call_args.args
    assert enqueued_id == returned_id


def test_upload_playlists_mint_distinct_task_ids_across_requests(
    client: TestClient,
) -> None:
    """Each upload must mint its own UUID — no reuse across requests."""
    with patch("backend.routers.ingestion.ingestion_task") as enqueue:
        ids: list[str] = []
        for i in range(3):
            resp = client.post(
                "/api/v1/ingestion/playlists",
                files={"file": (f"f{i}.csv", CSV_PAYLOAD, "text/csv")},
                data={"station_id": "00000000-0000-0000-0000-000000000001"},
            )
            assert resp.status_code == 202
            ids.append(resp.json()["task_id"])

    assert len(set(ids)) == 3, "Every upload must mint a distinct task_id"
    enqueue_ids = [call.args[3] for call in enqueue.call_args_list]
    assert enqueue_ids == ids, (
        "Each enqueue call must receive the same task_id the client got back"
    )
