"""Fast (fake-repo) twins of tests/integration/test_match_work_follows_grouping.py.

A match carries its file's work: grouping that gives a file its first work
moves the file's matches too. The Postgres file is the authority.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.catalog import Work
from backend.domain.enums import CatalogSource, MatchTier
from backend.domain.library import LibraryFile
from backend.domain.matching import Match
from backend.services.work_move_service import WorkMoveRepos, place_ungrouped_file
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _repos() -> WorkMoveRepos:
    files, works, masters, matches = (
        FakeLibraryFileRepository(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
        FakeMatchRepository(),
    )
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    return WorkMoveRepos(files=files, works=works, song_masters=masters, matches=matches)


def _work(repos: WorkMoveRepos, work_id: str) -> str:
    repos.works.upsert(Work(id=work_id, title=work_id, artist_id="a1", origin=CatalogSource.LOCAL))
    return work_id


def _file(repos: WorkMoveRepos, work_id: str | None) -> LibraryFile:
    return repos.files.upsert(
        LibraryFile(id=uuid4(), file_path=rf"D:\m\{uuid4()}.flac", format="flac", work_id=work_id)
    )


def _match(repos: WorkMoveRepos, file_id: UUID, work_id: str | None) -> Match:
    return repos.matches.create(
        Match(
            id=uuid4(),
            confidence_score=1.0,
            match_tier=MatchTier.MANUAL,
            identity_id=uuid4(),
            library_file_id=file_id,
            work_id=work_id,
        )
    )


def test_placing_an_ungrouped_file_moves_its_matches_to_the_work() -> None:
    repos = _repos()
    work = _work(repos, "kiss")
    file = _file(repos, None)
    match = _match(repos, file.id, None)

    place_ungrouped_file(file.id, work, repos.files, repos.matches)

    placed = repos.files.get_by_id(file.id)
    assert placed is not None and placed.work_id == work
    assert match.identity_id is not None
    moved = repos.matches.get_by_identity(match.identity_id)
    assert moved is not None and moved.work_id == work
