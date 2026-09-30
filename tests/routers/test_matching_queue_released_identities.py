"""Router (spec C2, ruling 25): an identity that a missing-file delete sends back to review
counts as likely, so GET /matching/queue?include_unlikely=false (the Resolution Center's
default) still shows its artist. An identity with no score and no such reason stays hidden."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from backend.domain.enums import MatchStatus
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import Conn, identity, library_file, match, work

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

QUEUE = "/api/v1/matching/queue?include_unlikely=false"


def _artist_of(db_conn: Conn, identity_id: UUID) -> str:
    row = db_conn.execute(
        "SELECT broadcast_artist_id FROM track_identities WHERE id = %s", (identity_id,)
    ).fetchone()
    assert row is not None
    return str(row["broadcast_artist_id"])


def test_an_identity_released_by_a_delete_keeps_its_artist_in_the_likely_queue(
    client: TestClient, db_conn: Conn
) -> None:
    repos = RepositoryFactory(db_conn)
    gone = library_file(repos, "/m/gone.flac", work(repos), missing=True)
    released = identity(repos)  # AUTO_MATCHED; this is its only match
    match(db_conn, released, gone.id)
    identity(repos, MatchStatus.NEEDS_REVIEW)  # the guard: no score, no reason code
    db_conn.commit()
    assert client.get(QUEUE).json()["items"] == []  # nothing likely before the delete

    deleted = client.request(
        "DELETE", "/api/v1/library/missing-files", json={"ids": [str(gone.id)]}
    )
    body = client.get(QUEUE).json()

    assert deleted.json()["matches_released"] == 1
    assert [i["id"] for i in body["items"]] == [_artist_of(db_conn, released)]
    assert body["unlikely_total"] == 1  # the guard's artist: still hidden, still counted
