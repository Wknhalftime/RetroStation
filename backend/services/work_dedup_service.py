"""Fold local works that duplicate one another into a single surviving work.

Grouping used to mint a second work for a title the artist already had (see
``grouping_service._create_local_work``). That is fixed going forward; this
service cleans up the duplicates it left behind. Two works are duplicates when
they share an artist and their titles are equal under the same comparison the
fuzzy matcher treats as a perfect score: ``strict_normalize(normalize_title())``.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from uuid import UUID

from backend.domain.catalog import WorkFootprint, WorkMergePlan
from backend.domain.enums import SelectionMethod
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.master_selection_service import reselect_master_from_files
from backend.services.normalization import normalize_title, strict_normalize


class WorkMergeError(Exception):
    """Base error for merging duplicate works."""


class MergeTargetNotFoundError(WorkMergeError):
    """Raised when a plan's target work no longer exists."""


def _dedup_key(footprint: WorkFootprint) -> tuple[str, str]:
    return footprint.artist_id, strict_normalize(normalize_title(footprint.title))


def _pick_survivor(members: Sequence[WorkFootprint]) -> WorkFootprint:
    """Choose the work whose id and title the merged work keeps.

    Everything moves to the survivor, so the title matters most: prefer one
    without a U+FFFD decode scar, then the spelling most of the group shares.
    Reference counts break the remaining ties (fewer rows to re-point), and
    the id makes the choice deterministic.
    """
    spelling_votes = Counter(m.title for m in members)
    return min(
        members,
        key=lambda m: (
            "�" in m.title,
            -spelling_votes[m.title],
            -m.file_count,
            -m.match_count,
            m.id,
        ),
    )


def plan_duplicate_merges(footprints: Sequence[WorkFootprint]) -> list[WorkMergePlan]:
    """Group duplicate works and name the survivor of each group."""
    groups: defaultdict[tuple[str, str], list[WorkFootprint]] = defaultdict(list)
    for footprint in footprints:
        key = _dedup_key(footprint)
        if key[1]:  # a title that normalizes to nothing matches nothing
            groups[key].append(footprint)

    plans: list[WorkMergePlan] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        survivor = _pick_survivor(members)
        plans.append(
            WorkMergePlan(
                target_id=survivor.id,
                source_ids=tuple(sorted(m.id for m in members if m.id != survivor.id)),
            )
        )
    return sorted(plans, key=lambda p: p.target_id)


def _manual_choice_to_carry(
    plan: WorkMergePlan, song_master_repo: SongMasterRepository,
) -> UUID | None:
    """Return a source's manual master file if the target has no manual master."""
    target_master = song_master_repo.get_by_work(plan.target_id)
    if target_master is not None and target_master.selection_method == SelectionMethod.MANUAL:
        return None
    for source_id in plan.source_ids:
        master = song_master_repo.get_by_work(source_id)
        if master is not None and master.selection_method == SelectionMethod.MANUAL:
            return master.preferred_file_id
    return None


def merge_work_group(
    plan: WorkMergePlan,
    *,
    work_repo: WorkRepository,
    song_master_repo: SongMasterRepository,
    library_file_repo: LibraryFileRepository,
) -> None:
    """Fold a planned duplicate group into its target work.

    Run inside one transaction per plan: the repository re-points references
    across several tables and a partial merge must not be committed.
    """
    if work_repo.get_by_id(plan.target_id) is None:
        raise MergeTargetNotFoundError(plan.target_id)
    carried = _manual_choice_to_carry(plan, song_master_repo)
    work_repo.merge_into(plan.target_id, plan.source_ids)
    reselect_master_from_files(
        plan.target_id, song_master_repo, library_file_repo, manual_file_id=carried,
    )
