"""The Missing Files endpoints own one transaction per request (spec C2).

A refused delete (409) and a driver error (500) roll back everything the request
wrote, and one request opens one connection, so the match release and the row
delete commit or roll back together.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import pytest

from backend.db import sync_conn
from backend.dependencies import SyncRepos, get_reconciliation_repos
from backend.domain.enums import FileStatus, MatchStatus
from backend.repositories.library_files import LibraryFileRepository
from backend.services.missing_file_reconciliation_service import ReconciliationRepos
from backend.services.repository_factory import RepositoryFactory, reconciliation_repos
from tests.integration.missing_file_seed import Conn, identity, library_file, match, work

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

URL = "/api/v1/library/missing-files"


class _FilesProxy:
    """A files port that runs *before_delete* just ahead of each delete_missing."""

    def __init__(self, files: LibraryFileRepository, before_delete: Callable[[UUID], None]):
        self._files = files
        self._before_delete = before_delete

    def __getattr__(self, name: str) -> Any:
        return getattr(self._files, name)

    def delete_missing(self, file_id: UUID) -> bool:
        self._before_delete(file_id)
        return self._files.delete_missing(file_id)


def _override_files(hook: Callable[[LibraryFileRepository, UUID], None]) -> Iterator[None]:
    from backend.main import app

    def _recon(repos: SyncRepos) -> ReconciliationRepos:
        recon = reconciliation_repos(repos)
        files = _FilesProxy(recon.files, lambda file_id: hook(recon.files, file_id))
        return replace(recon, files=files)  # type: ignore[arg-type]

    app.dependency_overrides[get_reconciliation_repos] = _recon
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_reconciliation_repos, None)


def _restore(files: LibraryFileRepository, file_id: UUID) -> None:
    """What a concurrent scan does to a row that reappeared on disk."""
    row = files.get_by_id(file_id)
    assert row is not None
    files.upsert(row)


def _deadlock(files: LibraryFileRepository, file_id: UUID) -> None:
    raise psycopg.errors.DeadlockDetected("simulated deadlock")


@pytest.fixture
def restored_mid_delete(client: TestClient) -> Iterator[None]:
    yield from _override_files(_restore)


@pytest.fixture
def deadlocked_mid_delete(client: TestClient) -> Iterator[None]:
    yield from _override_files(_deadlock)


def _seed(db_conn: Conn) -> tuple[UUID, UUID]:
    """One matched missing row; returns (file id, identity id)."""
    repos = RepositoryFactory(db_conn)
    gone = library_file(repos, "/m/gone.flac", work(repos), missing=True)
    identity_id = identity(repos)
    match(db_conn, identity_id, gone.id)
    db_conn.commit()
    return gone.id, identity_id


def _assert_untouched(db_conn: Conn, file_id: UUID, identity_id: UUID) -> None:
    repos = RepositoryFactory(db_conn)
    row = repos.library_files.get_by_id(file_id)
    released = repos.broadcast_identities.get_by_id(identity_id)
    assert row is not None and row.file_status == FileStatus.MISSING
    assert repos.matches.get_by_identity(identity_id) is not None
    assert released is not None and released.match_status == MatchStatus.AUTO_MATCHED


@pytest.mark.usefixtures("restored_mid_delete")
def test_a_row_restored_mid_delete_is_409_and_rolls_back_the_release(
    client: TestClient, db_conn: Conn
) -> None:
    file_id, identity_id = _seed(db_conn)

    response = client.request("DELETE", URL, json={"ids": [str(file_id)]})

    assert response.status_code == 409
    assert "no longer missing" in response.json()["detail"]
    _assert_untouched(db_conn, file_id, identity_id)


@pytest.mark.usefixtures("deadlocked_mid_delete")
def test_a_driver_error_mid_delete_is_500_and_rolls_back_the_release(
    client: TestClient, db_conn: Conn
) -> None:
    file_id, identity_id = _seed(db_conn)

    response = client.request("DELETE", URL, json={"ids": [str(file_id)]})

    assert response.status_code == 500
    _assert_untouched(db_conn, file_id, identity_id)


def test_a_failed_commit_is_a_500_not_a_success(
    client: TestClient, db_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transaction ends before the response goes out ("function" scope).

    Under FastAPI's default "request" scope the commit runs after the 200 is sent,
    and this test sees that 200.
    """
    file_id, identity_id = _seed(db_conn)
    real_connect = sync_conn.connect_sync

    def _commit_fails(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        conn = real_connect(dsn, **kwargs)

        def _raise() -> None:
            raise psycopg.OperationalError("simulated commit failure")

        conn.commit = _raise  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(sync_conn, "connect_sync", _commit_fails)

    response = client.request("DELETE", URL, json={"ids": [str(file_id)]})

    assert response.status_code == 500
    _assert_untouched(db_conn, file_id, identity_id)


def test_a_delete_opens_one_connection(
    client: TestClient, db_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id, _ = _seed(db_conn)
    opened: list[str] = []
    real_connect = sync_conn.connect_sync

    def _counting(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        opened.append(dsn)
        return real_connect(dsn, **kwargs)

    monkeypatch.setattr(sync_conn, "connect_sync", _counting)

    response = client.request("DELETE", URL, json={"ids": [str(file_id)]})

    assert response.status_code == 200
    assert len(opened) == 1
