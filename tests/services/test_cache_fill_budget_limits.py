"""The cache fill's request budget in its limits (phase 4.1 PR 2b; SPEC S8–S10; D10, D11;
AUD-R093).

D10, "One try + 10 s pause": each fill call makes one request, and after a refusal the fill waits
10 s before its next one. D11, "2 hours": a run hands on no batch once 120 minutes have passed
since it started. They live in ``FillLimits`` beside D5's batch, cut-off and caps.

The new fields default to D10's and D11's values, so ``FillLimits`` built with D5's four fields
alone equals ``FILL_LIMITS`` (``test_cache_fill_limits.py::test_the_limits_are_d5s`` keeps its
meaning), and this module pins the new values.
"""

from __future__ import annotations

from backend.domain.enums import FillTier
from backend.services.cache_fill_constants import FILL_LIMITS, FillLimits


def test_the_budget_is_d10s_and_d11s() -> None:
    assert FILL_LIMITS.attempts == 1
    assert FILL_LIMITS.pause_seconds == 10.0
    assert FILL_LIMITS.run_minutes == 120.0


def test_the_new_fields_default_to_d10s_and_d11s() -> None:
    d5s_fields_alone = FillLimits(
        batch_calls=100, batch_seconds=110.0, cutoff=5, caps={FillTier.PAYOFF: 2600}
    )

    assert d5s_fields_alone.attempts == 1
    assert d5s_fields_alone.pause_seconds == 10.0
    assert d5s_fields_alone.run_minutes == 120.0
