"""The guarded work merge: the only way two works merge (phase 3b PR 3; SPEC S3, D3; design
C58, C74; AUD-R079).

Inside a savepoint on the caller's connection, the guard reads what the group's lines play
through the air-play view, calls ``merge_works`` (which pins or stores what it can), reads
again, and keeps the merge only when every change was approved. A merge that goes through is
logged with its reason and approvals, and its merged-away ids redirect; one that fails is
undone alone and logged as failed.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from backend.domain.work_merges import (
    MergeChange,
    MergeDone,
    MergeRefused,
    WorkMergeFailedError,
    WorkMergeGroup,
    WorkMergeStorageError,
    all_approved,
    changes_between,
)
from backend.repositories.work_merges import WorkMergeRepository
from backend.services.work_curation_service import WorkCurationRepos, merge_works

logger = structlog.get_logger()


@dataclass(frozen=True)
class WorkMergeRepos:
    curation: WorkCurationRepos
    log: WorkMergeRepository


def guarded_merge(group: WorkMergeGroup, repos: WorkMergeRepos) -> MergeDone | MergeRefused:
    """Merge the group if nothing that plays changes unapproved; else undo it and say what."""
    try:
        return repos.log.in_savepoint(lambda: _attempt(group, repos))
    except WorkMergeStorageError as failure:
        merge_id = repos.log.log_failure(group, str(failure))
        logger.warning(
            "work_merge_failed",
            merge_id=str(merge_id),
            target_id=group.target_id,
            source_ids=list(group.source_ids),
            error=str(failure),
        )
        raise WorkMergeFailedError(f"Merge into {group.target_id} failed: {failure}") from failure


def preview(group: WorkMergeGroup, repos: WorkMergeRepos) -> tuple[MergeChange, ...]:
    """What the group's merge would change in air play, in a savepoint that is always undone:
    no log row, no capture, no redirect (3b PR 4, D27). A database refusal raises
    WorkMergeStorageError, logged nowhere."""

    def attempt() -> tuple[tuple[MergeChange, ...], bool]:
        lines = repos.log.lines_of_works([group.target_id, *group.source_ids])
        before = repos.log.line_results(lines)
        merge_works(group.target_id, list(group.source_ids), repos.curation)
        return changes_between(before, repos.log.line_results(lines)), False

    return repos.log.in_savepoint(attempt)


def _attempt(group: WorkMergeGroup, repos: WorkMergeRepos) -> tuple[MergeDone | MergeRefused, bool]:
    lines = repos.log.lines_of_works([group.target_id, *group.source_ids])
    before = repos.log.line_results(lines)
    merge_id = repos.log.start(group)
    result = merge_works(group.target_id, list(group.source_ids), repos.curation)
    repos.log.stop()
    changes = changes_between(before, repos.log.line_results(lines))
    if not all_approved(changes, group.approvals):
        return MergeRefused(changes), False
    gone = [w for w in group.source_ids if repos.curation.works.get_by_id(w) is None]
    repos.log.redirect(gone, group.target_id, merge_id)
    logger.info(
        "works_merged",
        merge_id=str(merge_id),
        target_id=group.target_id,
        source_ids=list(group.source_ids),
        reason=group.reason.value,
        changes=len(changes),
    )
    return MergeDone(merge_id, result, changes), True
