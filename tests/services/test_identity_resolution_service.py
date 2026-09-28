"""Unit tests for identity_resolution_service types (no database)."""

from __future__ import annotations

import dataclasses

import pytest

from backend.services.identity_resolution_service import RecalcRepos
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository


def test_recalc_repos_is_frozen() -> None:
    repos = RecalcRepos(
        song_masters=FakeSongMasterRepository(),
        recordings=FakeRecordingRepository(),
        library_files=FakeLibraryFileRepository(),
        commit=lambda: None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        repos.commit = lambda: None  # type: ignore[misc]
