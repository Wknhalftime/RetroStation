"""Remap (spec C2): which targets are refused, and that an accepted one is folded (fakes)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from backend.domain.library import (
    MissingFileNotFoundError,
    RemapTargetNotFoundError,
    RemapTargetNotPresentError,
    RemapTargetUngroupedError,
)
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    remap_missing_file,
)
from tests.services.missing_file_helpers import add_file, add_work, fake_repos


def test_an_accepted_remap_removes_the_missing_row() -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    target = add_file(repos, "/m/target.flac", missing=False)

    remap_missing_file(gone.id, target.id, repos)

    assert repos.files.get_by_id(gone.id) is None
    assert repos.files.get_by_id(target.id) is not None


def test_a_row_that_is_not_missing_cannot_be_remapped() -> None:
    repos = fake_repos()
    add_work(repos)
    here = add_file(repos, "/m/here.flac", missing=False)
    target = add_file(repos, "/m/target.flac", missing=False)

    with pytest.raises(MissingFileNotFoundError):
        remap_missing_file(here.id, target.id, repos)


_TARGETS: dict[str, Callable[[ReconciliationRepos], UUID]] = {
    "unknown": lambda _repos: uuid4(),
    "missing": lambda repos: add_file(repos, "/m/t.flac", missing=True).id,
    "ungrouped": lambda repos: add_file(repos, "/m/t.flac", missing=False, work_id=None).id,
}


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("unknown", RemapTargetNotFoundError),
        ("missing", RemapTargetNotPresentError),
        ("ungrouped", RemapTargetUngroupedError),
    ],
)
def test_a_target_must_be_a_grouped_file_on_disk(target: str, error: type[Exception]) -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)

    with pytest.raises(error):
        remap_missing_file(gone.id, _TARGETS[target](repos), repos)
    assert repos.files.get_by_id(gone.id) is not None  # nothing was folded
