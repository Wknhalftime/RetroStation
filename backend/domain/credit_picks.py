"""Credit picks: the proposals Lance applies, skips or picks (phase 3a PR 3; SPEC S8, S9, D4, D8,
D9; design C57, C64, C65, M27; AUD-R039, AUD-R076).

Pure rules only. A matched line whose log credit ``choose_performance`` answers with a
non-default performance gets one proposal: ``credit`` (applied as it is), ``version_line`` (a
line naming a version ``read_log_version`` recognises) or ``tie`` (two or more tied), the last
two for a per-line pick. A proposal is stale once its line's best match changed or what it would
store is gone, on another work or now the default; a stale row is never applied (C64).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.domain.enums import VersionType
from backend.domain.log_version_reader import read_log_version
from backend.domain.performances import (
    CreditedPerformance,
    PerformanceReason,
    choose_performance,
)


class CreditPickError(Exception):
    """Base of the Credit picks errors."""


class ProposalNotFoundError(CreditPickError):
    """No proposal has this id."""


class ProposalKindError(CreditPickError):
    """Apply takes only ``credit`` rows; a pick takes only version lines and ties."""


class ProposalTwiceError(CreditPickError):
    """One id was sent both to apply and to skip."""


class CandidateNotListedError(CreditPickError):
    """The performance picked isn't one of the row's candidates."""


class ProposalStaleError(CreditPickError):
    """The row's line or performance changed since it was proposed."""


class ProposalKind(StrEnum):
    CREDIT = "credit"
    VERSION_LINE = "version_line"
    TIE = "tie"


class ProposalState(StrEnum):
    PROPOSED = "proposed"
    SKIPPED = "skipped"
    APPLIED = "applied"
    PICKED = "picked"


@dataclass(frozen=True)
class LineCredit:
    """A matched line as the proposals read it: its best match, the performance that match
    stores, its work, its version text and its log name's active links."""

    identity_id: UUID
    match_id: UUID
    stored_performance_id: UUID | None
    work_id: str
    version_text: str | None
    artists: frozenset[str]


@dataclass(frozen=True)
class WorkCredits:
    """A work's performances with their credit artists, its default and its artist (D8)."""

    work_id: str
    artist_id: str | None
    default_performance_id: UUID | None
    performances: tuple[CreditedPerformance, ...]


@dataclass(frozen=True)
class ProposalAnswer:
    """What a run proposes for a line; a row keeps its id only while this stays the same."""

    match_id: UUID
    kind: ProposalKind
    reason: PerformanceReason
    performance_id: UUID | None
    candidates: tuple[UUID, ...]


@dataclass(frozen=True)
class Proposal:
    """A row of ``credit_pick_proposals``."""

    id: UUID
    identity_id: UUID
    match_id: UUID | None
    kind: ProposalKind
    reason: PerformanceReason
    performance_id: UUID | None
    candidates: tuple[UUID, ...]
    state: ProposalState

    @property
    def answer(self) -> ProposalAnswer | None:
        """What the row proposes; None once its match is gone (a stale row)."""
        if self.match_id is None:
            return None
        return ProposalAnswer(
            self.match_id, self.kind, self.reason, self.performance_id, self.candidates
        )


@dataclass(frozen=True)
class LineNow:
    """A line as it stands when Lance acts: its best match, work and the work's default."""

    identity_id: UUID
    match_id: UUID | None
    work_id: str | None
    default_performance_id: UUID | None


def names_a_version(version_text: str | None) -> bool:
    """Whether the line's version text reads as a version (2b's Unmapped page predicate)."""
    return version_text is not None and read_log_version(version_text) != VersionType.UNKNOWN


def propose(
    line: LineCredit, work: WorkCredits, primary_of: Mapping[str, str]
) -> ProposalAnswer | None:
    """The line's proposal, or None when its credit keeps the default (S8, C65)."""
    choice = choose_performance(
        line.artists,
        work.performances,
        work.default_performance_id,
        primary_of=primary_of,
        work_artist=work.artist_id,
    )
    if choice.reason in (PerformanceReason.DEFAULT, PerformanceReason.NO_PERFORMER):
        return None
    if choice.performance_id is None:
        kind = ProposalKind.VERSION_LINE if names_a_version(line.version_text) else ProposalKind.TIE
        return ProposalAnswer(line.match_id, kind, choice.reason, None, choice.tied)
    if choice.performance_id == line.stored_performance_id:
        return None
    if names_a_version(line.version_text):
        return ProposalAnswer(
            line.match_id,
            ProposalKind.VERSION_LINE,
            choice.reason,
            None,
            (choice.performance_id,),
        )
    return ProposalAnswer(
        line.match_id,
        ProposalKind.CREDIT,
        choice.reason,
        choice.performance_id,
        (choice.performance_id,),
    )


def still_valid(
    performance_id: UUID | None, now: LineNow, performance_works: Mapping[UUID, str]
) -> bool:
    """Whether *performance_id* may still be stored on the line: it exists, belongs to the
    line's work and isn't the default (C71)."""
    return (
        performance_id is not None
        and performance_works.get(performance_id) == now.work_id
        and performance_id != now.default_performance_id
    )


def live_candidates(
    proposal: Proposal, now: LineNow | None, performance_works: Mapping[UUID, str]
) -> tuple[UUID, ...]:
    """The candidates still non-default performances of the line's work; none when the line
    was re-resolved."""
    answer = proposal.answer
    if answer is None or now is None or now.match_id != answer.match_id:
        return ()
    return tuple(c for c in answer.candidates if still_valid(c, now, performance_works))


def is_stale(
    proposal: Proposal, now: LineNow | None, performance_works: Mapping[UUID, str]
) -> bool:
    """A ``credit`` row is stale when its line's best match changed or its performance may no
    longer be stored; a version line or tie when no candidate is left."""
    answer = proposal.answer
    if answer is None or now is None or now.match_id != answer.match_id:
        return True
    if answer.kind == ProposalKind.CREDIT:
        return not still_valid(answer.performance_id, now, performance_works)
    return not live_candidates(proposal, now, performance_works)
