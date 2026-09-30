"""Router (spec C2): /library/missing-files: list, remap, delete."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from backend.domain.enums import MatchStatus
from backend.domain.library import AudioMetadata
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import Conn, identity, library_file, match, work

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

URL = "/api/v1/library/missing-files"
_KISS = AudioMetadata(
    recording_mbid="rec-1",
    release_mbid="rel-1",
    duration_ms=54_040,
    artist_name="Prince",
    track_title="Kiss",
    release_title="Parade",
)


def _seed(db_conn: Conn) -> dict[str, UUID]:
    """Three missing copies of one track (one of them matched) and one present copy."""
    repos = RepositoryFactory(db_conn)
    work_id = work(repos)
    ids = {
        name: library_file(repos, f"/m/{name}.flac", work_id, missing=missing, audio=_KISS).id
        for name, missing in (("a-gone", True), ("b-gone", True), ("copy", False), ("lost", True))
    }
    ids["identity"] = identity(repos)
    match(db_conn, ids["identity"], ids["a-gone"])
    db_conn.commit()
    return ids


def test_lists_missing_files_with_matches_and_candidates(client: TestClient, db_conn: Conn) -> None:
    ids = _seed(db_conn)

    body = client.get(URL).json()

    assert (body["total"], body["total_match_count"]) == (3, 1)
    first = body["items"][0]
    assert first["file_path"] == "/m/a-gone.flac"
    assert (first["match_count"], first["work_title"], first["work_has_present_file"]) == (
        1,
        "Kiss",
        True,
    )
    assert [c["id"] for c in first["candidates"]] == [str(ids["copy"])]
    assert first["missing_since"] is not None


def test_pages_with_offset_and_limit(client: TestClient, db_conn: Conn) -> None:
    _seed(db_conn)

    body = client.get(URL, params={"offset": 1, "limit": 1}).json()

    assert [i["file_path"] for i in body["items"]] == ["/m/b-gone.flac"]
    assert body["total"] == 3


def test_limit_is_bounded(client: TestClient) -> None:
    assert client.get(URL, params={"limit": 0}).status_code == 422
    assert client.get(URL, params={"limit": 201}).status_code == 422


def test_remap_folds_the_row_into_the_chosen_file(client: TestClient, db_conn: Conn) -> None:
    ids = _seed(db_conn)

    response = client.post(
        f"{URL}/{ids['a-gone']}/remap", json={"target_file_id": str(ids["copy"])}
    )

    matched = db_conn.execute(
        "SELECT library_file_id FROM matches WHERE identity_id = %s", (ids["identity"],)
    ).fetchone()
    assert response.status_code == 204
    assert matched is not None and matched["library_file_id"] == ids["copy"]
    assert RepositoryFactory(db_conn).library_files.get_by_id(ids["a-gone"]) is None


def test_remap_of_an_unknown_row_is_404(client: TestClient, db_conn: Conn) -> None:
    ids = _seed(db_conn)

    response = client.post(f"{URL}/{uuid4()}/remap", json={"target_file_id": str(ids["copy"])})

    assert response.status_code == 404
    assert response.json()["detail"] != "Not Found"  # the endpoint's 404, not a missing route


def test_remap_onto_a_missing_file_is_409_and_changes_nothing(
    client: TestClient, db_conn: Conn
) -> None:
    ids = _seed(db_conn)

    response = client.post(
        f"{URL}/{ids['a-gone']}/remap", json={"target_file_id": str(ids["lost"])}
    )

    assert response.status_code == 409
    assert "missing from disk" in response.json()["detail"]
    assert RepositoryFactory(db_conn).library_files.get_by_id(ids["a-gone"]) is not None


def test_delete_by_ids_reports_and_releases_matches(client: TestClient, db_conn: Conn) -> None:
    ids = _seed(db_conn)

    response = client.request("DELETE", URL, json={"ids": [str(ids["a-gone"]), str(ids["copy"])]})

    identity_after = RepositoryFactory(db_conn).broadcast_identities.get_by_id(ids["identity"])
    assert response.status_code == 200
    assert response.json() == {"deleted": 1, "matches_released": 1, "skipped": 1}
    assert identity_after is not None and identity_after.match_status == MatchStatus.NEEDS_REVIEW


def test_delete_all_deletes_every_missing_row(client: TestClient, db_conn: Conn) -> None:
    _seed(db_conn)

    response = client.request("DELETE", URL, json={"all": True})

    assert response.json()["deleted"] == 3
    assert client.get(URL).json()["total"] == 0


def test_delete_needs_ids_or_all_but_not_both(client: TestClient) -> None:
    assert client.request("DELETE", URL, json={}).status_code == 422
    both = {"ids": [str(uuid4())], "all": True}
    assert client.request("DELETE", URL, json=both).status_code == 422
