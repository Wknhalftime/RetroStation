"""The merge list: the page, Library Status's count and Apply (work merges phase 3b PR 4; SPEC
S2, D6, D15, D26-D28, D37, E3, E5; scope Q11-Q13, Q22; design C57, C58, C74; AUD-R080).

Stateless (Q13): the page plans every group again and shows one page of them, 50 at a time,
most played first (D37). It previews each of the page's groups' guarded merge in a savepoint
that is always undone (D27 as D37 amends it) and shows what it would change. Apply plans again
and merges each group of the page Lance saw, as he saw it, through the guard, committing after
each (D26).

PR 5c (D40, D55-D58; AUD-R089): Lance's "Keep apart" marks. The planner never joins two kept
works; a group still holding both is held: shown with its pairs, not previewed, never applied.
Keep apart marks every pair of a group; Undo deletes one pair. The page lists every pair, and
the count counts them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

import structlog

from backend.domain.merge_list import (
    MERGE_LIST_NOTE,
    PAGE_SIZE,
    ApplyReport,
    DuplicateView,
    GroupOutcome,
    GroupResult,
    GroupView,
    KeepApartTooFewWorksError,
    KeepApartWorkNotFoundError,
    KeptApart,
    KeptApartView,
    LoggedFailure,
    MergeListCount,
    MergeListPage,
    NamedWork,
    RejectingLine,
    SeenGroup,
    WorkView,
    every_pair,
    failure_of,
    group_flags,
    works_in_order,
)
from backend.domain.work_merge_planner import PlannedGroup, WorkPair
from backend.domain.work_merges import (
    MergeChange,
    MergeReason,
    MergeRefused,
    WorkMergeFailedError,
    WorkMergeGroup,
    WorkMergeStorageError,
)
from backend.repositories.merge_list import MergeListRepository
from backend.repositories.performances import PerformanceRepository
from backend.repositories.user_settings import UserSettingRepository
from backend.repositories.work_keep_apart import WorkKeepApartRepository
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
    keep_apart: WorkKeepApartRepository


@dataclass(frozen=True)
class _PageReads:
    """What the page reads once for all its groups."""

    shown: dict[str, WorkView]
    rejections: list[RejectingLine]
    failures: list[LoggedFailure]


def _group(planned: PlannedGroup) -> WorkMergeGroup:
    return WorkMergeGroup(planned.target_id, planned.source_ids, MergeReason.MERGE_LIST)


def _view(planned: PlannedGroup, page: _PageReads, repos: MergeListRepos) -> GroupView:
    """One group as the page shows it, its changes from the guard's preview (D27); a held group
    isn't previewed (D56)."""
    ids = [planned.target_id, *planned.source_ids]
    works = works_in_order(planned.target_id, planned.source_ids, page.shown)
    changes: tuple[MergeChange, ...] = ()
    preview_error = None
    if not planned.held:
        try:
            changes = work_merge_service.preview(_group(planned), repos.merge)
        except WorkMergeStorageError as failed:
            preview_error = str(failed)
    return GroupView(
        target_id=planned.target_id,
        source_ids=planned.source_ids,
        joins=tuple(sorted(planned.joins)),
        plays=planned.plays,
        works=works,
        flags=group_flags(works, [r for r in page.rejections if r.work_id in ids]),
        changes=changes,
        failed=failure_of(planned.target_id, planned.source_ids, page.failures, preview_error),
        held=tuple(sorted(planned.held)),
    )


def _kept_view(
    mark: KeptApart, shown: Mapping[str, WorkView], mbids: Mapping[str, frozenset[str]]
) -> KeptApartView | None:
    first, second = mark.pair.first_work_id, mark.pair.second_work_id
    if first not in shown or second not in shown:
        return None
    return KeptApartView(
        first=shown[first],
        second=shown[second],
        kept_at=mark.kept_at,
        note=mark.note,
        musicbrainz_links=bool(mbids.get(first, frozenset()) & mbids.get(second, frozenset())),
    )


def page(repos: MergeListRepos, offset: int = 0, limit: int = PAGE_SIZE) -> MergeListPage:
    """One page of the planned groups, most played first (D37), each with its works, flags,
    changes (previewed for this page's groups only) and last failure; the recordings still under
    two works; the empty works kept; every kept pair (Q9), newest first."""
    marks = repos.keep_apart.marks()
    planned = plan(repos.merge_list, repos.settings, [m.pair for m in marks])
    on_page = planned.groups[offset : offset + limit]
    ids = [w for g in on_page for w in (g.target_id, *g.source_ids)]
    duplicates = repos.performances.duplicate_recordings()
    kept_ids = [w for m in marks for w in (m.pair.first_work_id, m.pair.second_work_id)]
    shown = repos.merge_list.works_shown(
        ids + [w for d in duplicates for w in d.work_ids] + kept_ids
    )
    reads = _PageReads(
        shown={w: replace(v, plays=planned.plays.get(w, 0)) for w, v in shown.items()},
        rejections=repos.merge_list.rejections(ids),
        failures=repos.merge_list.failures([g.target_id for g in on_page]),
    )
    return MergeListPage(
        total=len(planned.groups),
        offset=offset,
        limit=limit,
        groups=tuple(_view(g, reads, repos) for g in on_page),
        duplicates=tuple(
            DuplicateView(
                d.recording_mbid,
                tuple(NamedWork(w, shown[w].title if w in shown else "") for w in d.work_ids),
            )
            for d in duplicates
        ),
        kept_works=tuple(repos.works.kept_empty_works()),
        kept_apart=tuple(
            view for m in marks if (view := _kept_view(m, shown, planned.known_mbids)) is not None
        ),
    )


def count(repos: MergeListRepos) -> MergeListCount:
    """Library Status's count (D27, D28, D58): the planner alone, and the other sections'
    rows."""
    marks = repos.keep_apart.marks()
    return MergeListCount(
        total=len(plan_sets(repos.merge_list, repos.settings, [m.pair for m in marks])),
        duplicates=len(repos.performances.duplicate_recordings()),
        kept_works=len(repos.works.kept_empty_works()),
        kept_apart=len(marks),
    )


def keep_apart(work_ids: Sequence[str], repos: MergeListRepos) -> tuple[WorkPair, ...]:
    """Keep every pair of *work_ids* apart (D55), merged-away ids followed; a pair already kept
    keeps its row. The pairs of the group, in order."""
    live = repos.merge_list.live_ids(list(work_ids))
    nowhere = [w for w in work_ids if w not in live]
    if nowhere:
        raise KeepApartWorkNotFoundError(f"No such work: {', '.join(nowhere)}")
    pairs = every_pair([live[w] for w in work_ids])
    if not pairs:
        raise KeepApartTooFewWorksError("Keep apart needs two different works")
    repos.keep_apart.keep(pairs, MERGE_LIST_NOTE)
    return pairs


def undo_keep_apart(first_work_id: str, second_work_id: str, repos: MergeListRepos) -> None:
    """Keep the two works apart no more (D55); nothing when they aren't kept (Q3)."""
    if first_work_id != second_work_id:
        repos.keep_apart.undo(WorkPair.of(first_work_id, second_work_id))


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
    kept = [m.pair for m in repos.keep_apart.marks()]
    planned = [
        frozenset((g.target_id, *g.source_ids))
        for g in plan(repos.merge_list, repos.settings, kept).groups
        if not g.held  # D56: a held group is never applied
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
