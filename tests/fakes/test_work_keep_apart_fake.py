"""The keep-apart fake mirrors ``PgWorkKeepApartRepository`` (phase 3b PR 5c; the Pg side is
``tests/integration/test_work_keep_apart_pg.py``). Not locked."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from backend.domain.work_merge_planner import WorkPair
from tests.fakes.work_keep_apart import FakeWorkKeepApartRepository

START = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


def ticking() -> Iterator[datetime]:
    n = 0
    while True:
        yield START + timedelta(minutes=n)
        n += 1


def fake() -> FakeWorkKeepApartRepository:
    times = ticking()
    return FakeWorkKeepApartRepository(clock=lambda: next(times))


def test_a_pair_kept_twice_keeps_its_first_row_and_undo_deletes_it() -> None:
    marks = fake()
    pair = WorkPair.of("w-a", "w-b")

    marks.keep([pair], "first")
    marks.keep([pair], "second")

    [kept] = marks.marks()
    assert (kept.pair, kept.note, kept.kept_at) == (pair, "first", START)
    marks.undo(pair)
    marks.undo(pair)
    assert marks.marks() == []


def test_marks_come_newest_first_and_pairs_among_need_both_works() -> None:
    marks = fake()
    older, newer = WorkPair.of("w-a", "w-b"), WorkPair.of("w-a", "w-c")
    marks.keep([older], "")
    marks.keep([newer], "")

    assert [m.pair for m in marks.marks()] == [newer, older]
    assert marks.pairs_among(["w-a", "w-b", "w-x"]) == [older]
    assert marks.pairs_among(["w-b", "w-c"]) == []


def test_a_merge_carries_marks_drops_self_pairs_and_keeps_the_earlier() -> None:
    marks = fake()
    marks.keep([WorkPair.of("w-source", "w-x")], "the source's")  # earlier
    marks.keep([WorkPair.of("w-target", "w-x")], "the target's")
    marks.keep([WorkPair.of("w-source", "w-target")], "a kept pair merged")
    marks.keep([WorkPair.of("w-source", "w-y")], "moved")

    marks.carry(["w-source"], "w-target")

    assert {(m.pair, m.note) for m in marks.marks()} == {
        (WorkPair.of("w-target", "w-x"), "the source's"),
        (WorkPair.of("w-target", "w-y"), "moved"),
    }
