"""Fast (fake-repo) twins of tests/integration/test_enrichment_move_masters.py.

After enrichment moves a file to its MusicBrainz work, the work it left re-picks its
master from its own present files (an invalid manual pick becomes AUTO, ruling R1), or
has no master when no present file is left (ruling R2). The Postgres file is the
authority; these keep the rule in the fast CI split.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import EnrichmentRepos, enrich_by_release
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_RELEASE = "00000000-0000-4000-8000-000000000f01"
_RECORDING = "00000000-0000-4000-8000-000000000f02"
_ARTIST = "00000000-0000-4000-8000-000000000f03"
_WORK = "00000000-0000-4000-8000-000000000f04"

_RELEASE_DATA = {
    "id": _RELEASE,
    "title": "Test Album",
    "artist-credit": [{"artist": {"id": _ARTIST, "name": "Test Artist", "sort-name": "A, T"}}],
    "media": [
        {
            "tracks": [
                {
                    "recording": {
                        "id": _RECORDING,
                        "title": "Test Track",
                        "length": 240_000,
                        "relations": [
                            {"type": "performance", "work": {"id": _WORK, "title": "Test Work"}}
                        ],
                    }
                }
            ]
        }
    ],
}


class _World:
    """Fake repos wired so work deletion and file statuses behave as on Postgres."""

    def __init__(self) -> None:
        self.files = FakeLibraryFileRepository()
        self.works = FakeWorkRepository()
        self.works.set_library_file_repo(self.files)
        self.song_masters = FakeSongMasterRepository()
        self.song_masters.set_library_file_repo(self.files)
        self.repos = EnrichmentRepos(
            files=self.files,
            enrichment_queries=self.files,
            recordings=FakeRecordingRepository(),
            works=self.works,
            song_masters=self.song_masters,
            matches=FakeMatchRepository(),
            artists=FakeArtistRepository(),
        )
        self.old = self.works.create_local("Test Track", "local-artist")

    def file(self, *, moving: bool, missing: bool = False) -> LibraryFile:
        """A file on the old work; *moving* ones carry the tags enrichment resolves."""
        audio = AudioMetadata(
            recording_mbid=_RECORDING if moving else None,
            release_mbid=_RELEASE if moving else None,
        )
        lf = self.files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path=f"/music/{uuid4()}.flac",
                format="flac",
                work_id=self.old,
                audio=audio,
            )
        )
        if missing:
            self.files.mark_missing(lf.file_path)
        return lf

    def master(self, file_id: UUID, method: SelectionMethod) -> None:
        self.song_masters.upsert(
            SongMaster(
                id=uuid4(), work_id=self.old, preferred_file_id=file_id, selection_method=method
            )
        )

    def master_of(self, work_id: str) -> tuple[UUID, SelectionMethod] | None:
        master = self.song_masters.get_by_work(work_id)
        return None if master is None else (master.preferred_file_id, master.selection_method)

    def work_of(self, lf: LibraryFile) -> str:
        """The MusicBrainz work the file was moved to."""
        row = self.files.get_by_id(lf.id)
        assert row is not None and row.work_id is not None
        work = self.works.get_by_id(row.work_id)
        assert work is not None and work.mbid == _WORK
        return row.work_id

    def enrich(self) -> int:
        mb_client = FakeMbClient(releases={_RELEASE: _RELEASE_DATA})
        return enrich_by_release(_RELEASE, self.repos, mb_client)


@pytest.mark.parametrize("old_method", [SelectionMethod.AUTO, SelectionMethod.MANUAL])
def test_old_works_master_on_the_moved_file_ends_auto_on_the_file_it_keeps(
    old_method: SelectionMethod,
) -> None:
    """(a) AUTO and (b) MANUAL (ruling R1) on A: after A moves, the old master is AUTO on B."""
    world = _World()
    a = world.file(moving=True)
    b = world.file(moving=False)
    world.master(a.id, old_method)

    assert world.enrich() == 1

    assert world.master_of(world.old) == (b.id, SelectionMethod.AUTO)
    assert world.master_of(world.work_of(a)) == (a.id, SelectionMethod.AUTO)


@pytest.mark.parametrize("old_method", [SelectionMethod.AUTO, SelectionMethod.MANUAL])
def test_old_work_left_with_only_a_missing_file_has_no_master(
    old_method: SelectionMethod,
) -> None:
    """(d) Ruling R2: only missing M is left, so the old work keeps existing with no master."""
    world = _World()
    a = world.file(moving=True)
    world.file(moving=False, missing=True)
    world.master(a.id, old_method)

    assert world.enrich() == 1

    assert world.works.get_by_id(world.old) is not None
    assert world.master_of(world.old) is None
    assert world.master_of(world.work_of(a)) == (a.id, SelectionMethod.AUTO)
