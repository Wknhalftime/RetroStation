"""What a whole-name Reject or Unmatch does to the log name's lines (phase 1b, M24, AUD-R037).

``plan_cascade`` is the one place the rule lives: the actions, their preview and the counts
they answer with all ask it. It never takes a manual line or a line matched to a same-name file
off air (S1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from backend.domain.enums import CascadeAction, MatchStatus

# Lines play_file_resolution plays (migration 0030).
ON_AIR = frozenset({MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED})
# Lines the curator decided; a cascade leaves them as they are.
MANUAL = frozenset({MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED})


@dataclass(frozen=True)
class CascadeLine:
    """One line of a log name as a cascade sees it.

    ``same_name`` is whether the match the line plays is to a file whose artist is the name's
    (AUD-R022 D9, C16); ``plays`` counts the line's play events.
    """

    identity_id: UUID
    status: MatchStatus
    same_name: bool = False
    plays: int = 0

    @property
    def on_air(self) -> bool:
        return self.status in ON_AIR

    @property
    def kept(self) -> bool:
        """A manual line, or a line matched to a same-name file (S1)."""
        return self.status in MANUAL or (self.status == MatchStatus.AUTO_MATCHED and self.same_name)


@dataclass(frozen=True)
class CascadeCounts:
    """What goes off air and what stays on air (S5): the lines on air that change and their
    plays, and the lines on air that are kept, manual first, then same-name."""

    off_air_lines: int
    off_air_plays: int
    kept_manual: int
    kept_same_name: int


@dataclass(frozen=True)
class CascadePlan:
    action: CascadeAction
    kept: tuple[CascadeLine, ...]
    changed: tuple[CascadeLine, ...]

    def off_air(self) -> tuple[CascadeLine, ...]:
        """The changed lines that are on air now, which the action takes off air."""
        return tuple(line for line in self.changed if line.on_air)

    def counts(self) -> CascadeCounts:
        staying = [line for line in self.kept if line.on_air]
        manual = sum(1 for line in staying if line.status in MANUAL)
        return CascadeCounts(
            off_air_lines=len(self.off_air()),
            off_air_plays=sum(line.plays for line in self.off_air()),
            kept_manual=manual,
            kept_same_name=len(staying) - manual,
        )


def plan_cascade(action: CascadeAction, lines: Iterable[CascadeLine]) -> CascadePlan:
    """Which of the name's lines *action* keeps and which it changes, in the lines' order.

    Both actions keep the same lines; the caller writes the change (Reject: auto_rejected;
    Unmatch: back to review, matches deleted).
    """
    read = tuple(lines)
    return CascadePlan(
        action=action,
        kept=tuple(line for line in read if line.kept),
        changed=tuple(line for line in read if not line.kept),
    )
