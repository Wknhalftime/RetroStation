"""A missing row restored between its check and its delete aborts the whole deletion."""

from __future__ import annotations

from uuid import UUID

import pytest

from backend.domain.library import MissingFileChangedError, MissingFileSelection
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    delete_missing_files,
)
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository
from tests.services.missing_file_helpers import add_file, add_work, matched_identity


class _RestoredBeforeDelete(FakeLibraryFileRepository):
    """A concurrent scan restores every row just before its delete runs."""

    def delete_missing(self, file_id: UUID) -> bool:
        return False


def _racing_repos() -> ReconciliationRepos:
    files, works, masters = (
        _RestoredBeforeDelete(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
    )
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    return ReconciliationRepos(
        files=files,
        matches=FakeMatchRepository(),
        works=works,
        song_masters=masters,
        format_overrides=FakeFormatOverrideRepository(),
    )


def test_a_row_restored_before_its_delete_raises_instead_of_counting() -> None:
    repos, identities = _racing_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    matched_identity(repos, identities, gone.id)

    with pytest.raises(MissingFileChangedError):
        delete_missing_files(MissingFileSelection(ids=(gone.id,)), repos, identities)

    assert repos.files.get_by_id(gone.id) is not None
