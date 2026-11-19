"""The merge list: the page, Library Status's count and Apply (work merges phase 3b PR 4; SPEC
S2, D6, D15, D26-D28, E3, E5; scope Q11-Q13, Q22; design C57, C58, C74; AUD-R080).

Stateless (Q13): the page plans every group again, previews each group's guarded merge in a
savepoint that is always undone (D27), and shows what it would change. Apply plans again and
merges each group Lance saw, as he saw it, through the guard, committing after each (D26).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import structlog

from backend.domain.merge_list import (
    ApplyReport,
    DuplicateView,
    GroupOutcome,
    GroupResult,
    GroupView,
    LoggedFailure,
    MergeListCount,
    MergeListPage,
    NamedWork,
    RejectingLine,
    SeenGroup,
    WorkView,
    failure_of,
    group_flags,
    works_in_order,
)
from backend.domain.work_merge_planner import PlannedGroup
from backend.domain.work_merges import (
    MergeReason,
    MergeRefused,
    WorkMergeFailedError,
    WorkMergeGroup,
    WorkMergeStorageError,
)
from backend.repositories.merge_list import MergeListRepository
from backend.repositories.performances import PerformanceRepository
from backend.repositories.user_settings import UserSettingRepository
from backend.repositories.works import WorkRepository
from backend.services import work_merge_service
from backend.services.merge_plan_service import plan, plan_sets
from backend.services.work_merge_service import WorkMergeRepos

logger = structlog.get_logger()


@dataclass(frozen=True)
class MergeListRepos:
    merge_list: MergeListRepository
    settings: UserSettingRepository
    works: WorkRepository
    performances: PerformanceRepository
    merge: WorkMergeRepos


@dataclass(frozen=True)
class _PageReads:
    """What the page reads once for all its groups."""

    shown: dict[str, WorkView]
    rejections: list[RejectingLine]
    failures: list[LoggedFailure]


def _group(planned: PlannedGroup) -> WorkMergeGroup:
    return WorkMergeGroup(planned.target_id, planned.source_ids, MergeReason.MERGE_LIST)


def _view(planned: PlannedGroup, page: _PageReads, repos: MergeListRepos) -> GroupView:
    """One group as the page shows it, its changes from the guard's preview (D27)."""
    ids = [planned.target_id, *planned.source_ids]
    works = works_in_order(planned.target_id, planned.source_ids, page.shown)
    try:
        changes = work_merge_service.preview(_group(planned), repos.merge)
        preview_error = None
    except WorkMergeStorageError as failed:
        changes, preview_error = (), str(failed)
    return GroupView(
        target_id=planned.target_id,
        source_ids=planned.source_ids,
        joins=tuple(sorted(planned.joins)),
        plays=planned.plays,
        works=works,
        flags=group_flags(works, [r for r in page.rejections if r.work_id in ids]),
        changes=changes,
        failed=failure_of(planned.target_id, planned.source_ids, page.failures, preview_error),
    )


def page(repos: MergeListRepos) -> MergeListPage:
    """Every planned group with its works, flags, changes and last failure; the recordings
    still under two works; the empty works kept."""
    planned = plan(repos.merge_list, repos.settings)
    ids = planned.work_ids()
    duplicates = repos.performances.duplicate_recordings()
    shown = repos.merge_list.works_shown(ids + [w for d in duplicates for w in d.work_ids])
    reads = _PageReads(
        shown={w: replace(v, plays=planned.plays.get(w, 0)) for w, v in shown.items()},
        rejections=repos.merge_list.rejections(ids),
        failures=repos.merge_list.failures([g.target_id for g in planned.groups]),
    )
    return MergeListPage(
        groups=tuple(_view(g, reads, repos) for g in planned.groups),
        duplicates=tuple(
            DuplicateView(
                d.recording_mbid,
                tuple(NamedWork(w, shown[w].title if w in shown else "") for w in d.work_ids),
            )
            for d in duplicates
        ),
        kept_works=tuple(repos.works.kept_empty_works()),
    )


def count(repos: MergeListRepos) -> MergeListCount:
    """Library Status's count (D27, D28): the planner alone, and the other sections' rows."""
    return MergeListCount(
        total=len(plan_sets(repos.merge_list, repos.settings)),
        duplicates=len(repos.performances.duplicate_recordings()),
        kept_works=len(repos.works.kept_empty_works()),
    )


def _apply_one(
    seen: SeenGroup, planned: Sequence[frozenset[str]], repos: MergeListRepos
) -> GroupResult:
    live = repos.merge_list.live_ids([seen.target_id, *seen.source_ids])
    target = live.get(seen.target_id)
    if target is None or any(s not in live for s in seen.source_ids):
        return GroupResult(seen.target_id, seen.source_ids, GroupOutcome.CHANGED)
    sources = tuple(dict.fromkeys(live[s] for s in seen.source_ids if live[s] != target))
    if not sources:
        return GroupResult(seen.target_id, seen.source_ids, GroupOutcome.ALREADY_MERGED)
    works = {target, *sources}
    if not any(works <= ids for ids in planned):
        return GroupResult(seen.target_id, seen.source_ids, GroupOutcome.CHANGED)
    group = WorkMergeGroup(target, sources, MergeReason.MERGE_LIST, seen.approvals)
    try:
        outcome = work_merge_service.guarded_merge(group, repos.merge)
    except WorkMergeFailedError as failed:
        return GroupResult(seen.target_id, seen.source_ids, GroupOutcome.FAILED, error=str(failed))
    if isinstance(outcome, MergeRefused):
        return GroupResult(seen.target_id, seen.source_ids, GroupOutcome.WAITING, outcome.changes)
    return GroupResult(seen.target_id, seen.source_ids, GroupOutcome.MERGED, outcome.changes)


def apply(
    groups: Sequence[SeenGroup], repos: MergeListRepos, commit: Callable[[], None]
) -> ApplyReport:
    """Merge each group Lance saw (minus his skips), as he saw it, through the guard; commit
    after each one, a failed one's log row too (D26). A group whose works no longer plan
    together is left (``changed``), so nothing merges unseen."""
    planned = [
        frozenset((g.target_id, *g.source_ids))
        for g in plan(repos.merge_list, repos.settings).groups
    ]
    results: list[GroupResult] = []
    for seen in groups:
        results.append(_apply_one(seen, planned, repos))
        commit()
    report = ApplyReport(tuple(results))
    logger.info(
        "merge_list_applied",
        **{outcome.value: report.count(outcome) for outcome in GroupOutcome},
    )
    return report
