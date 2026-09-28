"""Unit tests for identity_matching_task's wiring (AUD-015).

These tests exercise only the wiring the task performs: which concrete
repository lands in which `IdentityMatchingRepos` field, and that
`settings.strong_match_threshold` reaches `match_identities_for_playlist`
unchanged. The repositories, DB connection and MB client are all replaced
with lightweight doubles via monkeypatch — no real Postgres connection is
opened. `match_identities_for_playlist` itself (the domain logic) is covered
separately in tests/services/test_identity_matching_service.py.

Swapping two repositories or losing a settings value in the dataclass
construction is exactly the risk this refactor (AUD-015) introduces, since
the task now builds one `IdentityMatchingRepos` object instead of passing
six positional/keyword repo arguments.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from backend.services.identity_matching_service import IdentityMatchingRepos
from backend.tasks.identity_matching_tasks import identity_matching_task


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def commit(self) -> None:
        pass


class _FakeMbCacheRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeTrackIdentityRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeBroadcastArtistRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeMatchRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeLibraryFileRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeMappingRuleRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeArtistCatalogRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeMbClient:
    def __init__(self, cache_repo: object, *, ttl_days: int) -> None:
        self.cache_repo = cache_repo
        self.ttl_days = ttl_days

    def __enter__(self) -> _FakeMbClient:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_identity_matching_task_wires_repos_and_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Pg*Repository must land in the matching `IdentityMatchingRepos`
    field, and `settings.strong_match_threshold` must reach the service call
    unmodified."""
    fake_settings = SimpleNamespace(
        database_url="postgresql://unused/unused",
        mb_cache_ttl_days=3650,
        strong_match_threshold=77,
    )
    monkeypatch.setattr("backend.tasks.identity_matching_tasks.get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.connect_sync",
        lambda *args, **kwargs: _FakeConn(),
    )
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.PgMusicBrainzCacheRepository",
        _FakeMbCacheRepo,
    )
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.PgBroadcastTrackIdentityRepository",
        _FakeTrackIdentityRepo,
    )
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.PgBroadcastArtistRepository",
        _FakeBroadcastArtistRepo,
    )
    monkeypatch.setattr("backend.tasks.identity_matching_tasks.PgMatchRepository", _FakeMatchRepo)
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.PgLibraryFileRepository",
        _FakeLibraryFileRepo,
    )
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.PgMappingRuleRepository",
        _FakeMappingRuleRepo,
    )
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.PgArtistRepository", _FakeArtistCatalogRepo
    )
    monkeypatch.setattr("backend.tasks.identity_matching_tasks.MusicBrainzApiClient", _FakeMbClient)

    captured: dict[str, Any] = {}

    def _fake_match_identities_for_playlist(
        *,
        playlist_id: object,
        repos: IdentityMatchingRepos,
        mb_client: object,
        strong_match_threshold: int,
    ) -> list[str]:
        captured["playlist_id"] = playlist_id
        captured["repos"] = repos
        captured["mb_client"] = mb_client
        captured["strong_match_threshold"] = strong_match_threshold
        return []

    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.match_identities_for_playlist",
        _fake_match_identities_for_playlist,
    )

    playlist_id = uuid4()
    identity_matching_task.call_local(str(playlist_id))

    assert captured["playlist_id"] == playlist_id
    assert captured["strong_match_threshold"] == 77
    assert captured["mb_client"].ttl_days == 3650
    assert isinstance(captured["mb_client"], _FakeMbClient)

    repos = captured["repos"]
    assert isinstance(repos, IdentityMatchingRepos)
    assert isinstance(repos.track_identity_repo, _FakeTrackIdentityRepo)
    assert isinstance(repos.broadcast_artist_repo, _FakeBroadcastArtistRepo)
    assert isinstance(repos.match_repo, _FakeMatchRepo)
    assert isinstance(repos.library_file_repo, _FakeLibraryFileRepo)
    assert isinstance(repos.rules_repo, _FakeMappingRuleRepo)
    assert isinstance(repos.catalog_repo, _FakeArtistCatalogRepo)
