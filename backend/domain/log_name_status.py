"""A log name's status from its links (artist model phase 1c, S3; C36, AUD-R038).

One rule, ``status_from_links``, decides the status every link change leaves the name in.
``LogNameLinkService`` asks it in the same transaction as the change, and migration 0045 applies
the same order in SQL. In order:

1. a user action writes the status it names (Resolve, Reject, Unmatch);
2. a ``manual_rejected`` or ``auto_rejected`` name keeps its status, whatever its links;
3. an active manual link: ``manual_matched``;
4. any other active link: ``auto_matched``;
5. a suggestion: ``needs_review`` / ``LINK_SUGGESTED``;
6. 7. 8. otherwise the writer's own answer stands (the matcher's ``LINK_REJECTED`` or
   ``DEFERRED_RETRY``); with none, a matched name left with no link is ``needs_review`` /
   ``LINK_REMOVED``, and any other name keeps the status its last writer set.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchStatus, ReasonCode
from backend.domain.matching import LogNameLink, MatchingError

_REJECTED = (MatchStatus.MANUAL_REJECTED, MatchStatus.AUTO_REJECTED)
_MATCHED = (MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED)


class UserActionWithoutStatusError(MatchingError):
    """A user action asked of the rule without the status it names."""


@dataclass(frozen=True)
class NameState:
    """A log name's status, its reason and the words that explain it."""

    status: MatchStatus
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


def suggestion_detail(strengths: Iterable[int]) -> str:
    """What a name's suggestions are, in words: how many, and the best one's score."""
    scores = list(strengths)
    plural = "" if len(scores) == 1 else "s"
    return f"{len(scores)} suggested artist{plural}, best {max(scores)}%"


def status_from_links(
    current: NameState,
    links: Iterable[LogNameLink],
    proposed: NameState | None = None,
    *,
    by_user: bool = False,
) -> NameState:
    """The status the name holds after a link change: *current* is its status before, *links*
    every link it holds after, *proposed* the status the writer names (a user action's, or the
    matcher's own answer) and *by_user* whether the writer is the curator."""
    if by_user:
        if proposed is None:
            raise UserActionWithoutStatusError("a user action names the status it writes")
        return proposed
    if current.status in _REJECTED:
        return current
    held = list(links)
    active = [link for link in held if link.state == LogNameLinkState.ACTIVE]
    if any(link.origin == LogNameLinkOrigin.MANUAL for link in active):
        return NameState(MatchStatus.MANUAL_MATCHED)
    if active:
        return NameState(MatchStatus.AUTO_MATCHED)
    suggested = [link.strength for link in held if link.state == LogNameLinkState.SUGGESTED]
    if suggested:
        return NameState(
            MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, suggestion_detail(suggested)
        )
    if proposed is not None:
        return proposed
    if current.status in _MATCHED:
        return NameState(
            MatchStatus.NEEDS_REVIEW,
            ReasonCode.LINK_REMOVED,
            "Its link was removed and nothing is suggested: pick an artist",
        )
    return current
