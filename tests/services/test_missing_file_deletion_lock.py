"""A deletion locks each missing row before touching it, and a purge names what it deleted."""

from __future__ import annotations

from uuid import UUID

from backend.domain.enums import MatchStatus
from backend.domain.library import AudioHash, AudioMetadata, MissingFileSelection
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    delete_missing_files,
    purge_unmatched_missing,
)
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository
from tests.services.missing_file_helpers import add_file, add_work, fake_repos, matched_identity

_HASH = AudioHash.parse("flac-md5:" + "c" * 32)


class _FoldedConcurrently(FakeLibraryFileRepository):
    """A concurrent fold took the row: the lock finds it no longer missing."""

    def lock_missing(self, file_id: UUID) -> bool:
        return False


def _locked_out_repos() -> ReconciliationRepos:
    files, works, masters = _FoldedConcurrently(), FakeWorkRepository(), FakeSongMasterRepository()
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    return ReconciliationRepos(
        files=files,
        matches=FakeMatchRepository(),
        works=works,
        song_masters=masters,
        format_overrides=FakeFormatOverrideRepository(),
    )


def test_a_row_the_lock_refuses_is_skipped_before_its_matches_are_released() -> None:
    repos, identities = _locked_out_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    identity_id = matched_identity(repos, identities, gone.id)

    result = delete_missing_files(MissingFileSelection(ids=(gone.id,)), repos, identities)

    after = identities.get_by_id(identity_id)
    assert (result.deleted, result.skipped, result.matches_released) == (0, 1, 0)
    assert repos.matches.get_by_identity(identity_id) is not None
    assert after is not None and after.match_status == MatchStatus.AUTO_MATCHED
    assert repos.files.get_by_id(gone.id) is not None


def test_a_repeated_id_is_deleted_once_and_skipped_once() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)

    result = delete_missing_files(MissingFileSelection(ids=(gone.id, gone.id)), repos, identities)

    assert (result.deleted, result.skipped) == (1, 1)


def test_a_purge_names_the_paths_it_deleted_and_counts_what_it_held_back() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    add_file(repos, "/lib/b.flac", missing=True)
    add_file(repos, "/lib/a.flac", missing=True, audio=AudioMetadata(track_title="Other"))
    # Held back: it has a fingerprint, and a present MP3 still waits for one.
    add_file(repos, "/lib/c.flac", missing=True, audio=AudioMetadata(), audio_hash=_HASH)
    add_file(repos, "/lib/d.mp3", missing=False, audio=AudioMetadata(), file_format="mp3")

    result = purge_unmatched_missing("/lib", repos, identities)

    assert result.deleted_paths == ("/lib/a.flac", "/lib/b.flac")
    assert (result.deleted, result.awaiting_fingerprint, result.held_back) == (2, 1, 1)
