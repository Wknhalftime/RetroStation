"""The merge planner's facts and plan (work merges phase 3b PR 4; SPEC S1, D29, Q22; AUD-R080).

Reads every work through the merge list's port, turns it into the planner's facts (grouping's
exact title key, credit artists by merge-group primary, normalized names, placeholders left
out), plans with the pure planner, and orders each group by matched plays. Refuses until D1's
credits are done (Q22).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from backend.domain.work_merge_planner import (
    CreditsNotReadyError,
    MergeSet,
    PlannedGroup,
    WorkFacts,
    merge_sets,
    plan_groups,
)
from backend.repositories.merge_list import MergeListRepository, PlanCredit, PlanWork
from backend.repositories.user_settings import UserSettingRepository
from backend.services.normalization import (
    canonicalize_part_number,
    extract_version_info,
    normalize_artist,
    normalize_title,
    strict_normalize,
)


def title_key(title: str) -> str:
    """Grouping's exact base-title rule (D29): version text stripped, normalized, the part
    number spelled one way, every character but letters, digits and spaces gone. Never its
    fuzzy match."""
    base, _ = extract_version_info(title)
    return strict_normalize(canonicalize_part_number(normalize_title(base)))


def require_credits(settings: UserSettingRepository) -> None:
    """Q22: the planner reads credits, so it waits for D1's marker."""
    if settings.get(WORK_ARTISTS_CREDITED_SETTING) is None:
        raise CreditsNotReadyError(
            "Credits not ready: the merge list waits for D1's step (credit_work_artists_task)"
        )


def _is_placeholder(credit: PlanCredit) -> bool:
    return credit.mbid is not None and credit.mbid in SPECIAL_PURPOSE_MBIDS


def _facts(work: PlanWork, primaries: Mapping[str, str]) -> WorkFacts:
    credits = [c for c in work.credits if not _is_placeholder(c)]
    artists = {
        primaries.get(c.artist_id, c.artist_id) if c.artist_id is not None else c.mbid
        for c in credits
    }
    names = {normalize_artist(name) for c in credits for name in c.names}
    return WorkFacts(
        work_id=work.work_id,
        title_key=title_key(work.title),
        work_mbid=work.work_mbid,
        linked_mbids=work.linked_mbids,
        artists=frozenset(a for a in artists if a),
        names=frozenset(n for n in names if n),
    )


def plan_sets(merge_list: MergeListRepository, settings: UserSettingRepository) -> list[MergeSet]:
    """The sets of works §6.2 joins, before their targets (Library Status's count, D27)."""
    require_credits(settings)
    works = merge_list.plan_works()
    artist_ids = {c.artist_id for w in works for c in w.credits if c.artist_id is not None}
    primaries = merge_list.primaries(artist_ids)
    return merge_sets(_facts(w, primaries) for w in works)


@dataclass(frozen=True)
class Plan:
    groups: tuple[PlannedGroup, ...]
    plays: Mapping[str, int]


def plan(merge_list: MergeListRepository, settings: UserSettingRepository) -> Plan:
    """Every planned group, most played first, with each work's matched plays."""
    sets = plan_sets(merge_list, settings)
    ids: Sequence[str] = sorted({w for s in sets for w in s.work_ids})
    plays = merge_list.matched_plays(ids) if ids else {}
    return Plan(tuple(plan_groups(sets, plays)), plays)
