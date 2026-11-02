"""Acceptance: production song matching runs in trial mode (SPEC 2026-11-01, S11; AUD-R100).

match_identities_for_playlist runs today's scorer alone unless it is given V11.1 (S10), and the
song-matching task is its one production caller. So the task, the composition root for song
matching, must pass V11.1, or trial mode never runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from backend.services.title_scoring import v11_title_score
from backend.tasks.identity_matching_tasks import identity_matching_task

_TASKS = "backend.tasks.identity_matching_tasks"
_REPOS = (
    "PgMusicBrainzCacheRepository",
    "PgBroadcastTrackIdentityRepository",
    "PgBroadcastArtistRepository",
    "PgMatchRepository",
    "PgLibraryFileRepository",
    "PgMappingRuleRepository",
    "PgArtistRepository",
)


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def commit(self) -> None:
        pass


class _Repo:
    def __init__(self, conn: object) -> None:
        self.conn = conn


class _MbClient:
    def __init__(self, cache_repo: object, *, ttl_days: int) -> None:
        self.cache_repo = cache_repo

    def __enter__(self) -> _MbClient:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_task_passes_v11(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        database_url="postgresql://unused/unused", mb_cache_ttl_days=3650, strong_match_threshold=80
    )
    monkeypatch.setattr(f"{_TASKS}.get_settings", lambda: settings)
    monkeypatch.setattr(f"{_TASKS}.connect_sync", lambda *args, **kwargs: _Conn())
    for name in _REPOS:
        monkeypatch.setattr(f"{_TASKS}.{name}", _Repo)
    monkeypatch.setattr(f"{_TASKS}.MusicBrainzApiClient", _MbClient)

    captured: dict[str, Any] = {}

    def match_identities_for_playlist(**kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(f"{_TASKS}.match_identities_for_playlist", match_identities_for_playlist)

    identity_matching_task.call_local(str(uuid4()))

    assert captured["trial_title_score"] is v11_title_score
