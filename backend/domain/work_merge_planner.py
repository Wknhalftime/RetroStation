"""The merge planner (work merges phase 3b PR 4; SPEC S1, D17's Q14-Q15, D29; design §6.2, M18,
M29; AUD-R080; PR 5c's kept pairs, D40, D55, D56, AUD-R089). Pure: facts in, groups out.

Two works join when they share a work MBID (their own, or one a cached recording lookup of their
performances names), or when their title keys are equal and they share a credit artist or a
credited name. A title alone never joins, and the title joins of one title key stand only while
the works they join hold at most one work MBID between them (Q15). Joins are transitive (Q14).

Two works Lance keeps apart (a :class:`WorkPair`, D40) are never joined to each other, by a work
link or a title. When other works still join them, their set is planned whole and *held*: it
names the kept pairs inside it, and Apply never merges it (D56).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class MergePlanError(Exception):
    """Base for the planner's refusals."""


class CreditsNotReadyError(MergePlanError):
    """The planner reads credits; until D1's step is done it can't trust them (Q22)."""


class WorkPairError(MergePlanError):
    """A kept pair must name two different works, the lower id first."""


@dataclass(frozen=True, order=True)
class WorkPair:
    """Two works Lance keeps apart, in plain string (code point) order: the lower id first, as
    the database stores them (``COLLATE "C"``)."""

    first_work_id: str
    second_work_id: str

    def __post_init__(self) -> None:
        if not self.first_work_id < self.second_work_id:
            raise WorkPairError(
                f"A kept pair needs two works, the lower first: "
                f"{self.first_work_id!r}, {self.second_work_id!r}"
            )

    @classmethod
    def of(cls, one: str, other: str) -> WorkPair:
        """The pair of *one* and *other*, in order."""
        first, second = sorted((one, other))
        return cls(first, second)


class MergeJoin(StrEnum):
    """Why two works of a group join (§6.2); an edge is named by its strongest reason."""

    WORK_LINK = "work_link"
    SHARED_ARTIST = "shared_artist"
    SHARED_NAME = "shared_name"


@dataclass(frozen=True)
class WorkFacts:
    """What the planner reads of one work. ``artists`` are its credits' artists, each as its
    merge-group primary, else as its MBID; ``names`` its credited names, normalized. Both leave
    placeholders out."""

    work_id: str
    title_key: str
    work_mbid: str | None
    linked_mbids: frozenset[str]
    artists: frozenset[str]
    names: frozenset[str]

    @property
    def known_mbids(self) -> frozenset[str]:
        """The work MBIDs this work is known to be: its own and those its lookups name."""
        own = frozenset({self.work_mbid}) if self.work_mbid is not None else frozenset()
        return own | self.linked_mbids


@dataclass(frozen=True)
class MergeSet:
    """Works joined by §6.2, before a target is chosen; ``held`` the kept pairs inside it."""

    work_ids: frozenset[str]
    musicbrainz_ids: frozenset[str]
    joins: frozenset[MergeJoin]
    held: frozenset[WorkPair] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PlannedGroup:
    """One planned merge: the sources into the target; ``plays`` are the group's matched plays;
    ``held`` the kept pairs that hold it (D56), empty for a group Apply may merge."""

    target_id: str
    source_ids: tuple[str, ...]
    joins: frozenset[MergeJoin]
    plays: int
    held: frozenset[WorkPair] = field(default_factory=frozenset)


class _Sets:
    """Union-find over work ids."""

    def __init__(self, ids: Iterable[str]) -> None:
        self._parent = {work_id: work_id for work_id in ids}

    def root(self, work_id: str) -> str:
        while self._parent[work_id] != work_id:
            self._parent[work_id] = self._parent[self._parent[work_id]]
            work_id = self._parent[work_id]
        return work_id

    def join(self, first: str, second: str) -> None:
        a, b = self.root(first), self.root(second)
        if a != b:
            self._parent[max(a, b)] = min(a, b)


type _Edge = tuple[str, str, MergeJoin]


def _kept(first: str, second: str, kept: frozenset[WorkPair]) -> bool:
    return first != second and WorkPair.of(first, second) in kept


def _link_edges(works: Sequence[WorkFacts], kept: frozenset[WorkPair]) -> list[_Edge]:
    """Every pair of works sharing a work MBID, minus the kept pairs (PR 5c's R1: a star from
    the first would strand the others when one of its edges is kept)."""
    by_mbid: dict[str, list[str]] = defaultdict(list)
    for work in works:
        for mbid in sorted(work.known_mbids):
            by_mbid[mbid].append(work.work_id)
    return [
        (first, second, MergeJoin.WORK_LINK)
        for ids in by_mbid.values()
        for n, first in enumerate(ids)
        for second in ids[n + 1 :]
        if not _kept(first, second, kept)
    ]


def _title_join(first: WorkFacts, second: WorkFacts) -> MergeJoin | None:
    if first.artists & second.artists:
        return MergeJoin.SHARED_ARTIST
    if first.names & second.names:
        return MergeJoin.SHARED_NAME
    return None


def _title_edges(works: Sequence[WorkFacts], kept: frozenset[WorkPair]) -> list[_Edge]:
    """The title joins of each title key, by the works they join, kept pairs left out first: a
    set of works one title key joins keeps its joins only while it holds at most one work MBID
    (Q15)."""
    by_key: dict[str, list[WorkFacts]] = defaultdict(list)
    for work in works:
        if work.title_key:
            by_key[work.title_key].append(work)
    edges: list[_Edge] = []
    for same_title in by_key.values():
        found = [
            (a.work_id, b.work_id, join)
            for n, a in enumerate(same_title)
            for b in same_title[n + 1 :]
            if not _kept(a.work_id, b.work_id, kept) and (join := _title_join(a, b)) is not None
        ]
        joined = _Sets(w.work_id for w in same_title)
        for first, second, _ in found:
            joined.join(first, second)
        mbids: dict[str, set[str]] = defaultdict(set)
        for work in same_title:
            mbids[joined.root(work.work_id)] |= work.known_mbids
        edges.extend(e for e in found if len(mbids[joined.root(e[0])]) <= 1)
    return edges


def merge_sets(works: Iterable[WorkFacts], kept_apart: Iterable[WorkPair] = ()) -> list[MergeSet]:
    """Every set of two or more works §6.2 joins, by their lowest work id; two works kept apart
    are never joined to each other, and a set still holding both is held (D40, D56)."""
    listed = sorted(works, key=lambda w: w.work_id)
    kept = frozenset(kept_apart)
    sets = _Sets(w.work_id for w in listed)
    edges = _link_edges(listed, kept) + _title_edges(listed, kept)
    for first, second, _ in edges:
        sets.join(first, second)
    members: dict[str, set[str]] = defaultdict(set)
    for work in listed:
        members[sets.root(work.work_id)].add(work.work_id)
    joins: dict[str, set[MergeJoin]] = defaultdict(set)
    for first, _, join in edges:
        joins[sets.root(first)].add(join)
    musicbrainz = {w.work_id for w in listed if w.work_mbid is not None}
    return [
        MergeSet(
            work_ids=frozenset(ids),
            musicbrainz_ids=frozenset(ids & musicbrainz),
            joins=frozenset(joins[root]),
            held=frozenset(p for p in kept if p.first_work_id in ids and p.second_work_id in ids),
        )
        for root, ids in sorted(members.items())
        if len(ids) > 1
    ]


def plan_groups(sets: Iterable[MergeSet], plays: Mapping[str, int]) -> list[PlannedGroup]:
    """Each set as a group: a MusicBrainz work first, then more plays, then the lower id; the
    groups most played first, then by target."""
    groups: list[PlannedGroup] = []
    for merge_set in sets:
        order = sorted(
            merge_set.work_ids,
            key=lambda w: (w not in merge_set.musicbrainz_ids, -plays.get(w, 0), w),
        )
        groups.append(
            PlannedGroup(
                target_id=order[0],
                source_ids=tuple(order[1:]),
                joins=merge_set.joins,
                plays=sum(plays.get(w, 0) for w in order),
                held=merge_set.held,
            )
        )
    return sorted(groups, key=lambda g: (-g.plays, g.target_id))
