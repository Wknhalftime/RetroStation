"""The cue coverage value (PR G2, Task 6; traceability L: T6.6, T6.7).

Requirements: D77 and D89 ("cues ready X of Y"); PG7 (analysable, ready, failed, unhashed);
H4 (value objects validate in ``__post_init__``, naming the field, so impossible counts cannot
be represented).
"""

from __future__ import annotations

import pytest

from backend.domain.streaming import CueCoverage, InvalidStreamValueError


@pytest.mark.parametrize(
    ("counts", "field"),
    [
        ({"analysable": 5, "ready": -1, "failed": 0, "unhashed": 0}, "CueCoverage.ready"),
        ({"analysable": 5, "ready": 1, "failed": 0, "unhashed": -2}, "CueCoverage.unhashed"),
        ({"analysable": 5, "ready": 4, "failed": 2, "unhashed": 0}, "CueCoverage.analysable"),
    ],
    ids=["negative-ready", "negative-unhashed", "settled-over-analysable"],
)
def test_coverage_refuses_impossible_counts_naming_the_field(
    counts: dict[str, int], field: str
) -> None:
    # T6.6 (H4, PG7): no count is negative, and no more audio is settled than is analysable.
    with pytest.raises(InvalidStreamValueError, match=field):
        CueCoverage(**counts)


def test_coverage_says_what_is_settled_and_waiting() -> None:
    # T6.7 (D89, PG7): settled is ready plus failed (both have a row, D20); waiting is the
    # rest of the analysable audio. Unhashed files are neither: they never get cues.
    coverage = CueCoverage(analysable=500, ready=120, failed=5, unhashed=7)
    assert (coverage.settled, coverage.waiting) == (125, 375)
    assert CueCoverage(analysable=0, ready=0, failed=0, unhashed=3).waiting == 0
