"""In-memory ``WorkKeepApartRepository`` (work merges phase 3b PR 5c, SPEC D40, Q4), mirroring
``PgWorkKeepApartRepository``: one mark per pair, the first kept stays; a merge's carry keeps
the earlier of two marks landing on one pair. A work's delete isn't modelled (the table's
CASCADE)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from backend.domain.merge_list import KeptApart
from backend.domain.work_merge_planner import WorkPair
from backend.repositories.work_keep_apart import WorkKeepApartRepository


def _now() -> datetime:
    return datetime.now(UTC)


class FakeWorkKeepApartRepository(WorkKeepApartRepository):
    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self._marks: dict[WorkPair, KeptApart] = {}
        self._clock = clock

    def keep(self, pairs: Sequence[WorkPair], note: str) -> None:
        for pair in pairs:
            self._marks.setdefault(pair, KeptApart(pair, self._clock(), note))

    def undo(self, pair: WorkPair) -> None:
        self._marks.pop(pair, None)

    def marks(self) -> list[KeptApart]:
        by_pair = sorted(self._marks.values(), key=lambda m: m.pair)
        return sorted(by_pair, key=lambda m: m.kept_at, reverse=True)  # stable: ties by pair

    def pairs_among(self, work_ids: Sequence[str]) -> list[WorkPair]:
        ids = set(work_ids)
        return sorted(p for p in self._marks if {p.first_work_id, p.second_work_id} <= ids)

    def carry(self, source_ids: Sequence[str], target_id: str) -> None:
        sources = set(source_ids)
        moved = [
            m
            for p, m in self._marks.items()
            if p.first_work_id in sources or p.second_work_id in sources
        ]
        for mark in moved:
            del self._marks[mark.pair]
        for mark in sorted(moved, key=lambda m: m.kept_at):
            ends = [
                target_id if w in sources else w
                for w in (mark.pair.first_work_id, mark.pair.second_work_id)
            ]
            if ends[0] == ends[1]:
                continue
            pair = WorkPair.of(*ends)
            kept = self._marks.get(pair)
            if kept is None or mark.kept_at < kept.kept_at:
                self._marks[pair] = KeptApart(pair, mark.kept_at, mark.note)
