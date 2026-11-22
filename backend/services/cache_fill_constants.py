"""The cache fill's limits (MusicBrainz cache fill, phase 4.1 PR 2; SPEC S4 and the budget
section's caps from D5's count; M1, M11; AUD-R091).

Per run, most plays first: payoff 2,600 (C8's common titles skipped, R12: about 47 minutes),
C68 300, C18 60, confirmations 500 (empty until 4.4, which sizes it) and tail 4,100 (about 75
minutes); the alias step is uncapped (43 requests at D5's count). A batch makes at most 100 calls
and starts none after 110 s, so it stays near 2 minutes even when calls fail slowly (R9); 5
transient failures in a row end the run. They live here, not in settings: they are the budget
Lance approved, not something to tune per machine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from backend.domain.enums import FillTier


@dataclass(frozen=True)
class FillLimits:
    """A run's caps per tier (a tier left out is uncapped) and a batch's limits."""

    batch_calls: int
    batch_seconds: float
    cutoff: int
    caps: Mapping[FillTier, int]

    def cap(self, tier: FillTier) -> int | None:
        """The tier's cap for a run, or None when it has none."""
        return self.caps.get(tier)


FILL_LIMITS = FillLimits(
    batch_calls=100,
    batch_seconds=110.0,
    cutoff=5,
    caps=MappingProxyType(
        {
            FillTier.PAYOFF: 2600,
            FillTier.C68: 300,
            FillTier.C18: 60,
            FillTier.CONFIRMATIONS: 500,
            FillTier.TAIL: 4100,
        }
    ),
)

ALIASES_PER_CALL = 100
"""MBIDs in one alias search: MusicBrainz caps a search's results at 100 (D3)."""

FILL_BATCH_PRIORITY = -10
"""Huey priority of a fill batch: below every other task (all at the default 0), so the one
worker (AUD-R017) runs anything else queued first."""
