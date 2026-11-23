"""The MusicBrainz cache fill's work and its run's state (phase 4.1 PR 2; SPEC S3, S4, S7; M1,
M2 as the coordinator read them with notes 1 and 2; D8; AUD-R091).

The work lists come from the database, one kind of row per tier; the fill turns each row into
one MusicBrainz call. A run is a chain of batches, and what the chain must remember from batch
to batch rides on it as a ``FillRun``: calls, failures and skips per tier, the keys tried and not
answered (asked once a run), the transient failures in a row, and the names each tier has filled
and not yet handed to a re-check. The cache can't hold that: a skipped call writes no row, and
song matching writes C18's keys too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from backend.domain.enums import FillTier


class CacheFillError(Exception):
    """Base class for the cache fill's errors."""


class MusicBrainzRefusingError(CacheFillError):
    """MusicBrainz refused too many of the run's calls in a row (a 4xx other than 429), with no
    answer between them: a block or a query bug, not one bad item. The batch ends FAILED, so the
    bar shows it until dismissed, instead of the run spending its caps on refusals."""


@dataclass(frozen=True)
class NamePair:
    """A searched log name with one of its titles (payoff, tail): a (name, normalized title)
    pair, its plays over all its lines, and the original title of its most-played line."""

    normalized_name: str
    original_name: str
    normalized_title: str
    title: str
    plays: int
    in_library: bool  # a present library file holds the title
    common: bool  # 3 or more distinct acts hold the title (C8)


@dataclass(frozen=True)
class UntaggedFile:
    """Present untagged files with one artist text and title, and how many there are (C68)."""

    artist_name: str
    title: str
    files: int


@dataclass(frozen=True)
class ManualTitle:
    """A title of a log name whose one active link is manual (C18), with that link's MBID."""

    normalized_name: str
    original_name: str
    artist_mbid: str
    title: str
    plays: int


@dataclass(frozen=True)
class QueuedPair:
    """A (log name, title) pair queued in ``mb_cache_fill_confirmations``."""

    normalized_name: str
    original_name: str
    title: str


def _counts(raw: Mapping[str, int] | None) -> dict[FillTier, int]:
    return {FillTier(tier): int(n) for tier, n in (raw or {}).items()}


def _plain(counts: Mapping[FillTier, int]) -> dict[str, int]:
    return {tier.value: counts.get(tier, 0) for tier in FillTier}


@dataclass(frozen=True)
class FillRun:
    """What a fill run carries from batch to batch (frozen: a batch returns a new one)."""

    started_at: str
    calls: Mapping[FillTier, int] = field(default_factory=dict)
    failed: Mapping[FillTier, int] = field(default_factory=dict)
    skipped: Mapping[FillTier, int] = field(default_factory=dict)
    missed: frozenset[str] = frozenset()
    in_a_row: int = 0  # transient failures in a row (the cut-off, E1)
    refused_in_a_row: int = 0  # permanent errors since the last answer
    filled: Mapping[FillTier, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def start(cls, at: datetime) -> FillRun:
        return cls(started_at=at.isoformat())

    def used(self, tier: FillTier) -> int:
        """Calls the run has made in *tier*, failed and skipped ones included."""
        return self.calls.get(tier, 0)

    def totals(self) -> dict[str, dict[str, int]]:
        """Calls, failures and skips per tier, for a progress row."""
        return {
            "calls": _plain(self.calls),
            "failed": _plain(self.failed),
            "skipped": _plain(self.skipped),
        }

    def handed_off(self, tiers: Iterable[FillTier]) -> FillRun:
        """The run with *tiers*' filled names dropped: their re-check is queued."""
        gone = set(tiers)
        return replace(
            self, filled={tier: names for tier, names in self.filled.items() if tier not in gone}
        )

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "started_at": self.started_at,
            **self.totals(),
            "missed": sorted(self.missed),
            "in_a_row": self.in_a_row,
            "refused_in_a_row": self.refused_in_a_row,
            "filled": {tier.value: sorted(names) for tier, names in self.filled.items()},
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> FillRun:
        payload = json.loads(text)
        return cls(
            started_at=str(payload["started_at"]),
            calls=_counts(payload.get("calls")),
            failed=_counts(payload.get("failed")),
            skipped=_counts(payload.get("skipped")),
            missed=frozenset(payload.get("missed", ())),
            in_a_row=int(payload.get("in_a_row", 0)),
            refused_in_a_row=int(payload.get("refused_in_a_row", 0)),
            filled={
                FillTier(tier): frozenset(names)
                for tier, names in (payload.get("filled") or {}).items()
            },
        )
