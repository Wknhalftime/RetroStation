"""Merged-away work ids redirect (artist model phase 3b PR 3, SPEC S3, Q19; AUD-R079).

``GET /api/v1/library/works/{id}`` of a work a merge took answers the merged work's detail (its
own id), however many merges followed, so a bookmark or an old link still opens the song; the
page replaces its URL (``AssociatedWorks.redirect.test.tsx``). An id no work ever had is still
404.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.integration import credit_picks_seed as cp
from tests.integration.work_merge_seed import TwoWorks

pytestmark = pytest.mark.integration

WORKS = "/api/v1/library/works"


def merge(client: TestClient, target: str, source: str) -> None:
    response = client.post(f"{WORKS}/{target}/merge", json={"source_work_ids": [source]})
    assert response.status_code == 200, response.text


def test_the_detail_of_a_merged_id_is_the_merged_work(
    client: TestClient, db_conn: psycopg.Connection[dict]
) -> None:
    world = TwoWorks(db_conn)
    db_conn.commit()
    merge(client, world.target, world.source)

    response = client.get(f"{WORKS}/{world.source}")

    assert response.status_code == 200
    assert response.json()["id"] == world.target
    assert response.json() == client.get(f"{WORKS}/{world.target}").json()


def test_a_chain_of_merges_resolves(client: TestClient, db_conn: psycopg.Connection[dict]) -> None:
    act = cp.artist(db_conn, "Bon Jovi")
    a, b, c = (cp.work(db_conn, "Always", act) for _ in range(3))
    for work in (a, b, c):
        cp.copy(db_conn, work, title="Always")
    db_conn.commit()
    merge(client, b, a)
    merge(client, c, b)

    response = client.get(f"{WORKS}/{a}")

    assert response.status_code == 200
    assert response.json()["id"] == c


def test_an_unknown_id_is_still_not_found(client: TestClient) -> None:
    assert client.get(f"{WORKS}/work-{uuid4()}").status_code == 404
