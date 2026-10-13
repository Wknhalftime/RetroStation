"""Splitting files out of a local work whose part number they do not share.

Grouping used to fuzzy-match "Another Brick in the Wall, Part 1" onto a
"..., Part II" work (the titles score 96). This service repairs the works
that left behind.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from backend.domain.catalog import PartSplitPlan
from backend.domain.enums import MatchTier
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services.normalization import normalize_artist
from backend.services.part_split_service import (
    PartSplitRepos,
    plan_part_splits,
    split_misplaced_parts,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_FLOYD = "Pink Floyd"
_BRICK = "Another Brick in the Wall, "


def _make_repos() -> PartSplitRepos:
    works = FakeWorkRepository()
    files = FakeLibraryFileRepository()
    artists = FakeArtistRepository()
    song_masters = FakeSongMasterRepository()
    works.set_artist_repo(artists)
    works.set_library_file_repo(files)
    song_masters.set_library_file_repo(files)
    return PartSplitRepos(
        artists=artists,
        works=works,
        files=files,
        song_masters=song_masters,
        matches=FakeMatchRepository(),
    )


def _work(repos: PartSplitRepos, title: str, artist: str = _FLOYD) -> str:
    artist_id = repos.artists.upsert_local_artist(artist, normalize_artist(artist))
    return repos.works.create_local(title, artist_id)


def _file(repos: PartSplitRepos, title: str, work_id: str, artist: str = _FLOYD) -> LibraryFile:
    lf = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{title}.flac",
        format="flac",
        work_id=work_id,
        audio=AudioMetadata(
            artist_name=artist,
            track_title=title,
            normalized_artist_name=normalize_artist(artist),
        ),
    )
    repos.files.upsert(lf)
    return lf


def _brick_work(repos: PartSplitRepos) -> tuple[str, dict[str, LibraryFile]]:
    """The reported state: four files on one "Part II" work."""
    work_id = _work(repos, _BRICK + "Part II")
    files = {
        part: _file(repos, _BRICK + part, work_id)
        for part in ("Part II", "Part 1", "Part 2", "Part 3")
    }
    return work_id, files


def test_plan_names_the_files_whose_part_differs_from_the_work_title() -> None:
    repos = _make_repos()
    work_id, files = _brick_work(repos)

    plans = plan_part_splits(repos.works, repos.files)

    assert plans == [
        PartSplitPlan(
            work_id=work_id,
            work_title=_BRICK + "Part II",
            file_ids=tuple(sorted((files["Part 1"].id, files["Part 3"].id), key=str)),
        )
    ]


def test_plan_skips_works_whose_files_agree() -> None:
    repos = _make_repos()
    work_id = _work(repos, "Song, Part II")
    _file(repos, "Song, Pt. 2", work_id)
    _file(repos, "Song, Part II (Live)", work_id)
    plain = _work(repos, "Hey Jude", artist="Beatles")
    _file(repos, "Hey Jude", plain, artist="Beatles")

    assert plan_part_splits(repos.works, repos.files) == []


def test_plan_leaves_a_work_without_a_part_in_its_title_alone() -> None:
    """Which part a bare "Shine On You Crazy Diamond" is, no title says."""
    repos = _make_repos()
    work_id = _work(repos, "Shine On You Crazy Diamond")
    _file(repos, "Shine On You Crazy Diamond (Part 1)", work_id)
    _file(repos, "Shine On You Crazy Diamond (Part 2)", work_id)

    assert plan_part_splits(repos.works, repos.files) == []


def test_split_moves_each_part_to_its_own_work() -> None:
    repos = _make_repos()
    work_id, files = _brick_work(repos)

    for plan in plan_part_splits(repos.works, repos.files):
        split_misplaced_parts(plan, repos)

    def work_title(part: str) -> str:
        moved = repos.files.get_by_id(files[part].id)
        assert moved is not None and moved.work_id is not None
        work = repos.works.get_by_id(moved.work_id)
        assert work is not None
        return work.title

    assert work_title("Part 1") == _BRICK + "Part 1"
    assert work_title("Part 3") == _BRICK + "Part 3"
    assert {f.id for f in repos.files.get_by_work(work_id)} == {
        files["Part II"].id,
        files["Part 2"].id,
    }
    assert plan_part_splits(repos.works, repos.files) == []


def test_split_joins_a_work_that_already_has_that_part() -> None:
    repos = _make_repos()
    part_1_work = _work(repos, _BRICK + "Part 1")
    work_id, files = _brick_work(repos)
    (plan,) = plan_part_splits(repos.works, repos.files)

    split_misplaced_parts(plan, repos)

    moved = repos.files.get_by_id(files["Part 1"].id)
    assert moved is not None
    assert moved.work_id == part_1_work


def test_split_takes_the_files_matches_along() -> None:
    repos = _make_repos()
    work_id, files = _brick_work(repos)
    match = Match(
        id=uuid4(),
        confidence_score=1.0,
        match_tier=MatchTier.MANUAL,
        identity_id=uuid4(),
        library_file_id=files["Part 1"].id,
        work_id=work_id,
    )
    repos.matches.create(match)
    (plan,) = plan_part_splits(repos.works, repos.files)

    split_misplaced_parts(plan, repos)

    moved = repos.files.get_by_id(files["Part 1"].id)
    assert moved is not None
    assert match.identity_id is not None
    assert repos.matches.get_by_identity(match.identity_id) == replace(match, work_id=moved.work_id)


def test_split_skips_a_file_that_left_the_work_since_planning() -> None:
    repos = _make_repos()
    work_id, files = _brick_work(repos)
    elsewhere = _work(repos, "Elsewhere")
    (plan,) = plan_part_splits(repos.works, repos.files)
    repos.files.update_work_id(files["Part 1"].id, elsewhere)

    split_misplaced_parts(plan, repos)

    moved = repos.files.get_by_id(files["Part 1"].id)
    assert moved is not None
    assert moved.work_id == elsewhere
