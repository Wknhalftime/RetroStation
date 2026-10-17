"""Fast (fake-repo) twin of the settle test in tests/integration/test_misplaced_song_masters.py.

A work whose master points at a file on another work is settled as the work a
file leaves: deleted once nothing references it, else its master is re-picked
from its own present files. The Postgres test is the authority.
"""

from __future__ import annotations

from uuid import uuid4

from backend.domain.catalog import Work
from backend.domain.curation import SongMaster
from backend.domain.enums import CatalogSource, SelectionMethod
from backend.domain.library import LibraryFile
from backend.services.work_move_service import WorkMoveRepos, settle_misplaced_masters
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _repos() -> WorkMoveRepos:
    files, works, masters = (
        FakeLibraryFileRepository(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
    )
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    return WorkMoveRepos(
        files=files, works=works, song_masters=masters, matches=FakeMatchRepository()
    )


def _work(repos: WorkMoveRepos, work_id: str) -> str:
    repos.works.upsert(Work(id=work_id, title=work_id, artist_id="a1", origin=CatalogSource.LOCAL))
    return work_id


def _file(repos: WorkMoveRepos, path: str, work_id: str) -> LibraryFile:
    return repos.files.upsert(
        LibraryFile(id=uuid4(), file_path=path, format="flac", work_id=work_id)
    )


def _auto_master(repos: WorkMoveRepos, work_id: str, file: LibraryFile) -> None:
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file.id,
            selection_method=SelectionMethod.AUTO,
        )
    )


def test_settles_only_works_whose_master_is_off_the_work() -> None:
    repos = _repos()
    home = _work(repos, "home")
    moved = _file(repos, r"D:\m\moved.flac", home)
    _auto_master(repos, home, moved)
    emptied = _work(repos, "emptied")
    _auto_master(repos, emptied, moved)
    kept = _work(repos, "kept")
    own = _file(repos, r"D:\m\own.flac", kept)
    _auto_master(repos, kept, moved)

    assert settle_misplaced_masters(repos) == [emptied, kept]

    assert repos.works.get_by_id(emptied) is None
    kept_master = repos.song_masters.get_by_work(kept)
    assert kept_master is not None
    assert kept_master.preferred_file_id == own.id
    home_master = repos.song_masters.get_by_work(home)
    assert home_master is not None
    assert home_master.preferred_file_id == moved.id
    # The fake work repo deletes the work but, unlike Postgres, not its master;
    # the integration twin checks that no master is left off its work.
