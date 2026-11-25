"""The cache fill's limits and names (MusicBrainz cache fill, phase 4.1 PR 2; SPEC S4, the budget
section's caps from D5's count; M1, M11; AUD-R091; PR 3's work links: S5, D13, AUD-R092).

Per run, most plays first: payoff 2,600 (C8's common titles skipped, R12), C68 300, C18 60,
confirmations 500 (empty until 4.4, which sizes it), work links 3,000 (PR 3, before the tail by
D13) and tail 4,100; the alias step is uncapped.
A batch makes at most 100 calls, starts none after 110 s, and 5 transient failures in a row end
the run. The constants live in their own module, not in settings, and the fill's task reads them.
"""

from __future__ import annotations

from backend.domain.enums import FillTier, TaskType
from backend.services.cache_fill_constants import FILL_LIMITS, FillLimits
from backend.tasks import cache_fill_tasks


def test_the_limits_are_d5s() -> None:
    d5s = FillLimits(
        batch_calls=100,
        batch_seconds=110.0,
        cutoff=5,
        caps={
            FillTier.PAYOFF: 2600,
            FillTier.C68: 300,
            FillTier.C18: 60,
            FillTier.CONFIRMATIONS: 500,
            FillTier.WORK_LINKS: 3000,
            FillTier.TAIL: 4100,
        },
    )
    assert d5s == FILL_LIMITS
    assert FillTier.ALIASES not in FILL_LIMITS.caps


def test_the_tiers_in_their_order() -> None:
    assert [tier.value for tier in FillTier] == [
        "payoff",
        "c68",
        "c18",
        "confirmations",
        "work_links",
        "tail",
        "aliases",
    ]


def test_the_task_types() -> None:
    assert TaskType.MB_CACHE_FILL.value == "mb_cache_fill"
    assert TaskType.NAMES_RECHECK.value == "names_recheck"


def test_the_fill_runs_with_them() -> None:
    assert cache_fill_tasks.FILL_LIMITS is FILL_LIMITS
