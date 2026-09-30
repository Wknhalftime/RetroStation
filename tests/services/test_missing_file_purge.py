"""Which missing rows an after-scan purge deletes, and which it keeps (spec C4, fakes)."""

from __future__ import annotations

from uuid import UUID

import pytest

from backend.domain.enums import PurgeMissingPolicy
from backend.domain.library import AudioHash, AudioMetadata
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    purge_unmatched_missing,
)
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.services.missing_file_helpers import add_file, add_work, fake_repos

_HASH = AudioHash.parse("flac-md5:" + "b" * 32)


def _purged(repos: ReconciliationRepos) -> set[UUID]:
    """Ids of the missing rows an after-scan purge of /lib deleted."""
    before = {f.id for f in repos.files.get_missing()}
    purge_unmatched_missing("/lib", repos, FakeBroadcastTrackIdentityRepository())
    return {i for i in before if repos.files.get_by_id(i) is None}


def test_a_missing_row_with_no_present_copy_is_purgeable() -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, "/lib/gone.flac", missing=True)

    assert _purged(repos) == {gone.id}


def test_a_row_whose_copy_is_present_but_ungrouped_is_kept() -> None:
    repos = fake_repos()
    add_work(repos)
    add_file(repos, "/lib/gone.flac", missing=True)
    add_file(repos, "/lib/new.flac", missing=False, work_id=None)

    assert _purged(repos) == set()


def test_an_ambiguous_row_is_kept() -> None:
    repos = fake_repos()
    add_work(repos)
    add_file(repos, "/lib/gone.flac", missing=True)
    add_file(repos, "/lib/a/copy.flac", missing=False)
    add_file(repos, "/lib/b/other.flac", missing=False)

    assert _purged(repos) == set()


def test_a_row_waiting_for_a_fingerprint_is_kept() -> None:
    repos = fake_repos()
    add_work(repos)
    add_file(repos, "/lib/gone.flac", missing=True, audio_hash=_HASH)
    add_file(repos, "/lib/comp/twin.flac", missing=False, audio_hash=_HASH)
    add_file(repos, "/lib/album/copy.mp3", missing=False, file_format="mp3")

    assert _purged(repos) == set()


def test_purge_deletes_only_missing_rows_under_the_scanned_root() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    inside = add_file(repos, "/lib/gone.flac", missing=True)
    outside = add_file(repos, "/other/x.flac", missing=True, audio=AudioMetadata())

    result = purge_unmatched_missing("/lib", repos, identities)

    assert result.deleted == 1
    assert repos.files.get_by_id(inside.id) is None
    assert repos.files.get_by_id(outside.id) is not None


def test_nothing_to_purge_deletes_nothing() -> None:
    result = purge_unmatched_missing("/lib", fake_repos(), FakeBroadcastTrackIdentityRepository())

    assert result.deleted == 0


@pytest.mark.parametrize(
    ("value", "policy"),
    [
        (None, PurgeMissingPolicy.NEVER),
        ("never", PurgeMissingPolicy.NEVER),
        ("after_scan", PurgeMissingPolicy.AFTER_SCAN),
        ("AFTER_SCAN", PurgeMissingPolicy.NEVER),
        ("yes", PurgeMissingPolicy.NEVER),
    ],
)
def test_the_policy_defaults_to_never(value: str | None, policy: PurgeMissingPolicy) -> None:
    assert PurgeMissingPolicy.from_setting(value) == policy
