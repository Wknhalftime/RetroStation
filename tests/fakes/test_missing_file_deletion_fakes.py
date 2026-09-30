"""The fakes' deletes mirror PG."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import MatchTier, SelectionMethod
from backend.domain.library import LibraryFile
from backend.domain.matching import Match
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository


def test_fake_delete_missing_mirrors_pg() -> None:
    repo = FakeLibraryFileRepository()
    gone = repo.upsert(LibraryFile(id=uuid4(), file_path="/m/gone.flac", format="flac"))
    here = repo.upsert(LibraryFile(id=uuid4(), file_path="/m/here.flac", format="flac"))
    repo.mark_missing(gone.file_path)

    assert (repo.delete_missing(gone.id), repo.delete_missing(here.id)) == (True, False)
    assert repo.get_by_id(gone.id) is None and repo.get_by_id(here.id) is not None


def test_fake_match_delete_for_file_mirrors_pg() -> None:
    repo = FakeMatchRepository()
    gone, other, identity, artist = uuid4(), uuid4(), uuid4(), uuid4()
    for file_id in (gone, other):
        repo.create(
            Match(
                id=uuid4(),
                confidence_score=1.0,
                match_tier=MatchTier.MANUAL,
                identity_id=identity,
                library_file_id=file_id,
            )
        )
    repo.create(
        Match(
            id=uuid4(),
            confidence_score=1.0,
            match_tier=MatchTier.MANUAL,
            artist_id=artist,
            library_file_id=gone,
        )
    )

    assert repo.delete_for_file(gone) == [identity]
    assert repo.get_by_identity(identity) is not None
    assert repo.get_by_artist(artist) is None


def test_fake_master_delete_for_file_mirrors_pg() -> None:
    masters, gone, kept = FakeSongMasterRepository(), uuid4(), uuid4()
    for work_id, file_id in (("a", gone), ("b", kept), ("c", gone)):
        masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work_id,
                preferred_file_id=file_id,
                selection_method=SelectionMethod.AUTO,
                score=0,
            )
        )

    assert sorted(masters.delete_for_file(gone)) == ["a", "c"]
    assert [masters.get_by_work(w) is not None for w in ("a", "b", "c")] == [False, True, False]


def test_fake_override_delete_for_file_mirrors_pg() -> None:
    overrides, gone, kept = FakeFormatOverrideRepository(), uuid4(), uuid4()
    for work_id, file_id in (("a", gone), ("b", kept)):
        overrides.create(
            FormatOverride(id=uuid4(), work_id=work_id, format_name="f", preferred_file_id=file_id)
        )

    overrides.delete_for_file(gone)

    assert (len(overrides.list_by_work("a")), len(overrides.list_by_work("b"))) == (0, 1)
