"""Performances: one recording of a work, with its credit, version and length (phase 3a; design
§6.1-6.3, C40, C44, C69, C71; AUD-R032, AUD-R075).

Pure rules only. A file joins a performance of its own work when it has a recording MBID and
the performance has the same one, or, with no recording MBID, when the performance has the
same credit artists, version and a length within 3 s of its ``length_ms`` (SPEC S2, question
E). Credit artists are compared as sets of MBIDs, placeholders left out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.enums import CreditRole, FileStatus, VersionType

LENGTH_TOLERANCE_MS = 3_000
FEATURING_WORDS = frozenset({"feat.", "feat", "ft.", "ft", "featuring"})


@dataclass(frozen=True)
class CreditPart:
    """One entry of a file's ``credit``: its MBID, credited name and following joining words."""

    mbid: str
    name: str | None = None
    join: str | None = None


@dataclass(frozen=True)
class PerformanceKey:
    """What S2's rule compares: a file's, or a performance's from its first file."""

    recording_mbid: str | None
    credit_mbids: frozenset[str]
    version_type: VersionType
    length_ms: int | None


@dataclass(frozen=True)
class FileForPerformance:
    """The columns of a file S2's rule reads, and where the file sits now."""

    id: UUID
    work_id: str | None
    performance_id: UUID | None
    recording_mbid: str | None
    title: str | None
    length_ms: int | None
    credit: tuple[CreditPart, ...]


@dataclass(frozen=True)
class CopyRow:
    """A copy's columns the master rules read."""

    file_id: UUID
    file_status: FileStatus
    release_status: str | None
    release_type: str | None
    format: str | None
    bitrate: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class StoredLine:
    """A match storing a performance, and the work of the file it is matched to."""

    match_id: UUID
    line_work_id: str | None


@dataclass(frozen=True)
class PerformanceCandidate:
    """A performance of the file's work, with the lowest id among its files (the tie-break)."""

    id: UUID
    key: PerformanceKey
    first_file_id: UUID | None


@dataclass(frozen=True)
class NewCredit:
    position: int
    artist_id: str | None
    mbid: str
    credited_name: str | None
    join_after: str | None
    role: CreditRole


@dataclass(frozen=True)
class CopyScore:
    """A copy of a performance, ranked for its preferred copy by the master rules."""

    file_id: UUID
    file_status: FileStatus
    score: tuple[int, int, int]


class StoredLineOutcome(StrEnum):
    KEPT = "kept"
    HANDED_OFF = "handed_off"
    RELEASED = "released"


def credit_mbids(parts: Iterable[CreditPart]) -> frozenset[str]:
    """The credit's artists, placeholders left out (C7)."""
    return frozenset(p.mbid for p in parts if p.mbid not in SPECIAL_PURPOSE_MBIDS)


def joins_by_length(length_ms: int | None) -> bool:
    """A NULL or negative length never joins by length."""
    return length_ms is not None and length_ms >= 0


def distance_to(key: PerformanceKey, theirs: PerformanceKey) -> int | None:
    """How far a file's *key* is from a performance it would join, or None when it doesn't.

    The same recording MBID joins at 0; a file with an MBID joins nothing else. A file with no
    MBID joins at the length difference when the credit artists and version are the same and
    both lengths are usable and within 3 s (question E).
    """
    if key.recording_mbid is not None:
        return 0 if key.recording_mbid == theirs.recording_mbid else None
    if theirs.credit_mbids != key.credit_mbids or theirs.version_type != key.version_type:
        return None
    if key.length_ms is None or theirs.length_ms is None:
        return None
    if not (joins_by_length(key.length_ms) and joins_by_length(theirs.length_ms)):
        return None
    distance = abs(theirs.length_ms - key.length_ms)
    return distance if distance <= LENGTH_TOLERANCE_MS else None


def fits(key: PerformanceKey, theirs: PerformanceKey) -> bool:
    """Whether a file with *key* still belongs to the performance keyed *theirs* (D6)."""
    return distance_to(key, theirs) is not None


def find_performance(
    key: PerformanceKey, candidates: Sequence[PerformanceCandidate]
) -> UUID | None:
    """The performance of the work the file joins, or None for a new one (S2, question E):
    the closest, then the one whose first file has the lower id."""
    fitting: list[tuple[int, str, UUID]] = []
    for candidate in candidates:
        distance = distance_to(key, candidate.key)
        if distance is not None:
            first = str(candidate.first_file_id) if candidate.first_file_id else "~"
            fitting.append((distance, first, candidate.id))
    return min(fitting)[2] if fitting else None


def credit_role(join_before: str | None) -> CreditRole:
    """Featured after "feat." and the like (C69), else main."""
    if join_before is not None and join_before.strip().lower() in FEATURING_WORDS:
        return CreditRole.FEATURED
    return CreditRole.MAIN


def new_credits(
    parts: Sequence[CreditPart], artists_by_mbid: dict[str, tuple[str, str]]
) -> list[NewCredit]:
    """A new performance's credits from its first file's credit (C44, D1, question A).

    *artists_by_mbid* maps an MBID to the (id, name) of the artist holding it.
    """
    kept = [p for p in parts if p.mbid not in SPECIAL_PURPOSE_MBIDS]
    credits: list[NewCredit] = []
    for position, part in enumerate(kept):
        artist = artists_by_mbid.get(part.mbid)
        join_before = kept[position - 1].join if position > 0 else None
        artist_name = artist[1] if artist else None
        credits.append(
            NewCredit(
                position=position,
                artist_id=artist[0] if artist else None,
                mbid=part.mbid,
                credited_name=part.name if part.name is not None else artist_name,
                join_after=part.join,
                role=credit_role(join_before),
            )
        )
    return credits


def best_copy(copies: Sequence[CopyScore]) -> UUID | None:
    """The preferred copy among *copies*: present before missing, then the master rules, then
    the lower file id (S5)."""
    if not copies:
        return None
    present = [c for c in copies if c.file_status == FileStatus.PRESENT]
    pool = present or list(copies)
    best = max(pool, key=lambda c: (c.score, _descending(c.file_id)))
    return best.file_id


def _descending(file_id: UUID) -> tuple[int, ...]:
    """Sort key making the lower id win under ``max``."""
    return tuple(-b for b in file_id.bytes)


class PerformanceReason(StrEnum):
    """Why ``choose_performance`` answered as it did (C47)."""

    CREDIT = "credit"
    SHARED_ARTIST = "shared_artist"
    DEFAULT = "default"
    NO_PERFORMER = "no_performer"


@dataclass(frozen=True)
class CreditedPerformance:
    """A performance of the work and its credit's artists (ids, or ``mbid:`` keys, D1)."""

    id: UUID
    artists: frozenset[str]


@dataclass(frozen=True)
class PerformanceChoice:
    """The performance a line's credit picks, or none; ``tied`` holds the performances that tie
    at the best, for Lance's per-line pick (D3)."""

    reason: PerformanceReason
    performance_id: UUID | None
    tied: tuple[UUID, ...] = ()


def choose_performance(
    credit_artists: Iterable[str],
    performances: Iterable[CreditedPerformance],
    default: UUID | None,
    *,
    primary_of: Mapping[str, str] | None = None,
    work_artist: str | None = None,
) -> PerformanceChoice:
    """The performance a log line's credit picks inside its work (S6, D3, D8, D9; C47, M21).

    Every artist compares as its merge-group primary. A performance answers better when it
    misses fewer of the line's artists, then credits fewer others; the default wins any tie with
    the best. An uncredited default credits the work's artist (D8). A line with no linked artist
    gets the default (C16).
    """
    groups = primary_of or {}
    line = frozenset(groups.get(a, a) for a in credit_artists)
    if not line:
        return PerformanceChoice(PerformanceReason.DEFAULT, default)
    scored: list[tuple[tuple[int, int], UUID]] = []
    default_score: tuple[int, int] | None = None
    for candidate in performances:
        artists = frozenset(groups.get(a, a) for a in candidate.artists)
        if candidate.id == default and not artists and work_artist is not None:
            artists = frozenset({groups.get(work_artist, work_artist)})
        if not artists & line:
            continue
        score = (len(line - artists), len(artists - line))
        if candidate.id == default:
            default_score = score
        else:
            scored.append((score, candidate.id))
    return _best_answer(scored, default, default_score)


def _best_answer(
    scored: list[tuple[tuple[int, int], UUID]],
    default: UUID | None,
    default_score: tuple[int, int] | None,
) -> PerformanceChoice:
    """D9: the default wins a tie with the best; tied non-defaults go to Lance."""
    best = min([s for s, _ in scored] + ([default_score] if default_score else []), default=None)
    if best is None:
        return PerformanceChoice(PerformanceReason.NO_PERFORMER, None)
    if default_score == best:
        return PerformanceChoice(PerformanceReason.DEFAULT, default)
    reason = PerformanceReason.CREDIT if best[0] == 0 else PerformanceReason.SHARED_ARTIST
    winners = sorted(performance_id for score, performance_id in scored if score == best)
    if len(winners) == 1:
        return PerformanceChoice(reason, winners[0])
    return PerformanceChoice(reason, None, tuple(winners))


def stored_line_outcome(
    *,
    line_work_id: str | None,
    performance_work_id: str,
    emptied: bool,
    landing: frozenset[tuple[UUID, str]],
) -> tuple[StoredLineOutcome, UUID | None]:
    """What happens to a line storing a performance a writer touched (E3, D5, question B).

    *landing* is the set of (performance, work) its former files now sit in.
    """
    if not emptied and line_work_id == performance_work_id:
        return StoredLineOutcome.KEPT, None
    if emptied and len(landing) == 1:
        [(target, target_work)] = landing
        if target_work == line_work_id:
            return StoredLineOutcome.HANDED_OFF, target
    return StoredLineOutcome.RELEASED, None
