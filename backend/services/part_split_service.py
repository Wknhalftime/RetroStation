"""Move files out of a local work whose part number they do not share.

Grouping used to fuzzy-match "Another Brick in the Wall, Part 1" onto an
existing "..., Part II" work: the titles differ by one character and scored 96
against a threshold of 80. That is fixed going forward (see
``grouping_service._fuzzy_match_work``); this service repairs the works it
left behind. A file belongs on its work only when both titles carry the same
part number or neither carries one: "The Unforgiven" moves off "The
Unforgiven III", and "Shine On You Crazy Diamond (Part 1)" off the bare
"Shine On You Crazy Diamond".
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from backend.domain.catalog import PartSplitPlan
from backend.domain.library import LibraryFile
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.grouping_service import find_or_create_work
from backend.services.normalization import (
    extract_version_info,
    normalize_title,
    title_part_number,
)
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work

logger = structlog.get_logger()


@dataclass(frozen=True)
class PartSplitRepos:
    """The repositories a split reads works from and moves files through."""

    artists: ArtistCatalogRepository
    works: WorkRepository
    files: LibraryFileRepository
    song_masters: SongMasterRepository
    matches: MatchRepository


def _part_of(title: str) -> int | None:
    """The part number grouping reads from a title, after stripping version tags."""
    base_title, _ = extract_version_info(title)
    return title_part_number(normalize_title(base_title))


def _misplaced(work_part: int | None, files: list[LibraryFile]) -> tuple[LibraryFile, ...]:
    """The files whose title's part number (or lack of one) differs from ``work_part``."""
    return tuple(f for f in files if _part_of(f.audio.track_title or "") != work_part)


def plan_part_splits(
    work_repo: WorkRepository,
    library_file_repo: LibraryFileRepository,
) -> list[PartSplitPlan]:
    """Every local work holding files whose part number differs from its title's."""
    plans: list[PartSplitPlan] = []
    for footprint in work_repo.list_local_footprints():
        work_part = _part_of(footprint.title)
        misplaced = _misplaced(work_part, library_file_repo.get_by_work(footprint.id))
        if misplaced:
            plans.append(
                PartSplitPlan(
                    work_id=footprint.id,
                    work_title=footprint.title,
                    file_ids=tuple(sorted((f.id for f in misplaced), key=str)),
                )
            )
    return plans


def split_misplaced_parts(plan: PartSplitPlan, repos: PartSplitRepos) -> None:
    """Move each planned file to the work its own title names.

    That work is found or created exactly as grouping would place the file
    today. A file that has left the planned work since planning is skipped.
    """
    move_repos = WorkMoveRepos(
        files=repos.files,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
    )
    for library_file in repos.files.get_by_ids(list(plan.file_ids)):
        if library_file.work_id != plan.work_id:
            continue
        target = find_or_create_work(
            library_file,
            artist_repo=repos.artists,
            work_repo=repos.works,
            song_master_repo=repos.song_masters,
        )
        move_file_to_work(library_file, target, move_repos)
        logger.info(
            "part_file_split_from_work",
            file_id=str(library_file.id),
            from_work=plan.work_id,
            to_work=target,
        )
