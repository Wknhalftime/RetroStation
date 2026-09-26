"""Plan and apply repairs for library rows left behind by case-only renames.

Before every case-only rename was recognised as one, a rename that also
retagged the file, or renamed its folder, left a second row for the same
file under the old spelling, often still holding the file's matches. On a
case-insensitive disk both spellings open the file; the row spelled as the
disk spells it is the one scans keep, so it is the one that survives.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from backend.domain.library import CaseDuplicateRepair, LibraryFile
from backend.repositories.library_files import LibraryFileRepository


def on_disk_spelling(file_path: str) -> str | None:
    """*file_path* as the disk spells it; None if no file is there or it can't be told.

    realpath reports the disk's spelling. When it differs by more than case
    (a junction, a subst drive, 8.3 short names) the spelling can't be read
    from it, and no row can be picked as the right one.
    """
    if not os.path.isfile(file_path):
        return None
    real = os.path.realpath(file_path)
    if os.path.normcase(real) != os.path.normcase(file_path):
        return None
    return real


def plan_case_duplicate_repairs(
    groups: list[list[LibraryFile]],
    spelling: Callable[[str], str | None] = on_disk_spelling,
) -> tuple[list[CaseDuplicateRepair], list[list[str]]]:
    """Pick each group's keeper: the one row spelled as the file is on disk.

    Returns the repairs, and the groups (as paths) where no single row is
    spelled that way: the file is gone, or its spelling can't be read.
    """
    repairs: list[CaseDuplicateRepair] = []
    unresolved: list[list[str]] = []
    for group in groups:
        disk = spelling(group[0].file_path)
        keepers = [f for f in group if f.file_path == disk]
        if len(keepers) != 1:
            unresolved.append([f.file_path for f in group])
            continue
        keeper = keepers[0]
        repairs.append(CaseDuplicateRepair(
            keeper_id=keeper.id,
            keeper_path=keeper.file_path,
            stale_ids=tuple(f.id for f in group if f.id != keeper.id),
        ))
    return repairs, unresolved


def apply_case_duplicate_repair(
    repair: CaseDuplicateRepair, file_repo: LibraryFileRepository,
) -> None:
    """Fold each stale row into the keeper."""
    for stale_id in repair.stale_ids:
        file_repo.merge_into(stale_id, repair.keeper_id)
