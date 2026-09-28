"""Unit tests for artist_matching_task's wiring (AUD-040).

Exercises only the wiring the task performs: which concrete repository lands
in which `ArtistMatchingRepos` field, and that each `settings.*` threshold
lands in the matching `ArtistMatchThresholds` field. All five threshold
values are set to distinct integers so a swap (e.g. mb_score_gap and
mb_auto_link_score) would fail the assertions. Repositories, the DB
connection and the MB client are lightweight doubles via monkeypatch — no
real Postgres connection is opened, and the fire-and-forget
`identity_matching_task` enqueue at the end is replaced with a no-op so this
test never touches the Huey SQLite queue.

`match_artists_for_playlist` itself (the domain logic) is covered separately
in tests/services/test_artist_matching_service.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from backend.services.artist_matching_service import ArtistMatchingRepos, ArtistMatchThresholds
from backend.tasks.artist_matching_tasks import artist_matching_task


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


class _FakeBroadcastArtistRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    def get_all_for_playlist(self, playlist_id: object) -> list[object]:
        return []

    def reset_deferred_by_ids(self, artist_ids: list[object]) -> int:
        return 0


class _FakeTrackIdentityRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    def reset_deferred_by_artist_ids(self, artist_ids: list[object]) -> int:
        return 0


class _FakeArtistRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeMatchRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _FakeMappingRuleRepo:
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


def test_artist_matching_task_wires_repos_and_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Pg*Repository must land in the matching `ArtistMatchingRepos`
    field, and each `settings.*` threshold must land in the matching
    `ArtistMatchThresholds` field — distinct values catch any swap."""
    fake_settings = SimpleNamespace(
        database_url="postgresql://unused/unused",
        mb_cache_ttl_days=3650,
        strong_match_threshold=71,
        mb_score_gap=12,
        mb_auto_link_score=93,
        min_presentation_score=55,
        broadcast_name_max_len=33,
    )
    monkeypatch.setattr("backend.tasks.artist_matching_tasks.get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.connect_sync",
        lambda *args, **kwargs: _FakeConn(),
    )
    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.PgMusicBrainzCacheRepository", _FakeMbCacheRepo
    )
    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.PgBroadcastArtistRepository",
        _FakeBroadcastArtistRepo,
    )
    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.PgBroadcastTrackIdentityRepository",
        _FakeTrackIdentityRepo,
    )
    monkeypatch.setattr("backend.tasks.artist_matching_tasks.PgArtistRepository", _FakeArtistRepo)
    monkeypatch.setattr("backend.tasks.artist_matching_tasks.PgMatchRepository", _FakeMatchRepo)
    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.PgMappingRuleRepository", _FakeMappingRuleRepo
    )
    monkeypatch.setattr("backend.tasks.artist_matching_tasks.MusicBrainzApiClient", _FakeMbClient)
    # Fire-and-forget enqueue at the end of the task — replace with a no-op so
    # this test never touches the Huey SQLite queue.
    monkeypatch.setattr(
        "backend.tasks.identity_matching_tasks.identity_matching_task",
        lambda playlist_id: None,
    )

    captured: dict[str, Any] = {}

    def _fake_match_artists_for_playlist(
        *,
        playlist_id: object,
        repos: ArtistMatchingRepos,
        mb_client: object,
        thresholds: ArtistMatchThresholds,
    ) -> None:
        captured["playlist_id"] = playlist_id
        captured["repos"] = repos
        captured["mb_client"] = mb_client
        captured["thresholds"] = thresholds

    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.match_artists_for_playlist",
        _fake_match_artists_for_playlist,
    )

    playlist_id = uuid4()
    artist_matching_task.call_local(str(playlist_id))

    assert captured["playlist_id"] == playlist_id
    assert isinstance(captured["mb_client"], _FakeMbClient)
    assert captured["mb_client"].ttl_days == 3650

    repos = captured["repos"]
    assert isinstance(repos, ArtistMatchingRepos)
    assert isinstance(repos.broadcast_artist_repo, _FakeBroadcastArtistRepo)
    assert isinstance(repos.track_identity_repo, _FakeTrackIdentityRepo)
    assert isinstance(repos.artist_repo, _FakeArtistRepo)
    assert isinstance(repos.match_repo, _FakeMatchRepo)
    assert isinstance(repos.rules_repo, _FakeMappingRuleRepo)

    thresholds = captured["thresholds"]
    assert isinstance(thresholds, ArtistMatchThresholds)
    assert thresholds.strong_match_threshold == 71
    assert thresholds.mb_score_gap == 12
    assert thresholds.mb_auto_link_score == 93
    assert thresholds.min_presentation_score == 55
    assert thresholds.broadcast_name_max_len == 33
