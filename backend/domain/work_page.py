"""What the Library's work page shows (artist model phase 3b PR 5a; SPEC S8, D41; design C61,
C65, C71, C72).

:func:`work_page` is pure: from the work's performances, its master and its lines (one row per
station and line, as the air-play view resolves it) it orders the performances (the master's
first, the default, C71), counts the lines each stored performance plays, lists under each copy
the lines it holds through a merge pin with what each plays next, names the work's credited
artists, and lists its acts when its performances credit two or more. The read models of the
rest of the page (files, recordings, master, overrides) live here too, so the service and the
router share them without the router's schemas.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from backend.domain.enums import FileStatus
from backend.domain.version_pages import MasterFlag, WorkVersion

ORIGINAL = "original"


class PlaySource(StrEnum):
    """The step of the air-play order a line's file came from (``play_file_resolution.source``,
    C61)."""

    PERFORMANCE = "performance"
    VERSION = "version"
    OVERRIDE = "override"
    PIN = "pin"
    MASTER = "master"


@dataclass(frozen=True)
class CreditName:
    """One credited artist as the page names it: its artist id (none for a credit by MBID
    alone, 3a's D1), its name, and the phrase joining it to the next."""

    artist_id: str | None
    name: str
    join_after: str | None = None


@dataclass(frozen=True)
class PerformanceFacts:
    id: UUID
    credit: tuple[CreditName, ...]
    version_type: str
    preferred_copy_id: UUID | None
    copy_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class MasterCopy:
    file_id: UUID
    path: str


@dataclass(frozen=True)
class WorkLine:
    """One station's resolution of one line of the work."""

    identity_id: UUID
    title: str
    artist: str
    station_id: UUID
    station: str
    station_format: str | None
    plays: int
    file_id: UUID | None
    path: str | None
    source: PlaySource | None
    pinned_file_id: UUID | None


@dataclass(frozen=True)
class PinnedLine:
    identity_id: UUID
    station_id: UUID
    station: str
    title: str
    artist: str
    plays: int
    next_file_id: UUID | None
    next_path: str | None


@dataclass(frozen=True)
class CopyView:
    file_id: UUID
    pins: tuple[PinnedLine, ...] = ()


@dataclass(frozen=True)
class PerformanceView:
    id: UUID
    credit: tuple[CreditName, ...]
    credit_text: str
    version_type: str
    default: bool
    preferred_copy_id: UUID | None
    lines: int
    copies: tuple[CopyView, ...]


@dataclass(frozen=True)
class Act:
    """The performances that credit one set of artists. The default act's lines are the lines on
    the default path, which an override or version entry reaches (``reached``, by station
    format); another act's are the lines that store its performance, which nothing set on the
    work reaches (C61, C65)."""

    credit: str
    default: bool
    lines: int
    reached: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkPage:
    performances: tuple[PerformanceView, ...]
    artists: tuple[CreditName, ...]
    acts: tuple[Act, ...]


# --- the rest of the page's read models -------------------------------------------------------


@dataclass(frozen=True)
class WorkHead:
    """The work a page shows: a merged-away id reads as its merged work (3b S3)."""

    id: str
    title: str
    artist_id: str


@dataclass(frozen=True)
class OtherWork:
    """The work a listed file is on, when it is not the page's (a shared recording)."""

    id: str
    title: str
    artist_name: str
    artists: tuple[str, ...]


@dataclass(frozen=True)
class PageFile:
    id: UUID
    file_path: str
    format: str
    bitrate: int | None
    duration_ms: int | None
    track_title: str | None
    release_title: str | None
    enrichment_status: str
    file_status: FileStatus
    other_work: OtherWork | None = None


@dataclass(frozen=True)
class PageRecording:
    id: str
    title: str
    version_type: str
    duration_ms: int | None
    files: tuple[PageFile, ...]


@dataclass(frozen=True)
class PageMaster:
    id: UUID
    preferred_file_id: UUID
    selection_method: str
    score: int | None
    updated_at: datetime


@dataclass(frozen=True)
class PageOverride:
    id: UUID
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


@dataclass(frozen=True)
class WorkDetailPage:
    """Everything ``GET /works/{id}`` answers."""

    id: str
    title: str
    artist_id: str
    recordings: tuple[PageRecording, ...]
    song_master: PageMaster | None
    format_overrides: tuple[PageOverride, ...]
    versions: tuple[WorkVersion, ...] = ()
    master_flag: MasterFlag | None = None
    page: WorkPage = WorkPage((), (), ())
    copies: Mapping[UUID, PageFile] = field(default_factory=dict)


# --- the rules --------------------------------------------------------------------------------


def credit_text(parts: Sequence[CreditName]) -> str:
    """The credit as one text: each name, then its own join phrase, else a comma."""
    text = ""
    for index, part in enumerate(parts):
        text += part.name
        if index < len(parts) - 1:
            text += part.join_after if part.join_after is not None else ", "
    return text


def _artist_key(part: CreditName) -> str:
    return part.artist_id if part.artist_id is not None else part.name


def _act_key(performance: PerformanceFacts) -> frozenset[str]:
    return frozenset(_artist_key(p) for p in performance.credit)


def _order(
    performances: Iterable[PerformanceFacts], default: UUID | None
) -> list[PerformanceFacts]:
    """The default first; then by credit; the original before other versions, by name; then
    by id."""
    return sorted(
        performances,
        key=lambda p: (
            p.id != default,
            credit_text(p.credit).casefold(),
            p.version_type != ORIGINAL,
            p.version_type,
            p.id,
        ),
    )


def _next(line: WorkLine, master: MasterCopy | None) -> tuple[UUID | None, str | None]:
    """What the line plays once its pin is released: the master while it plays its pin, else
    what it plays now (a step read before the pin already wins, C61)."""
    if line.source == PlaySource.PIN:
        return (master.file_id, master.path) if master is not None else (None, None)
    return line.file_id, line.path


def _pins(lines: Sequence[WorkLine], master: MasterCopy | None) -> dict[UUID, list[PinnedLine]]:
    pins: dict[UUID, list[PinnedLine]] = defaultdict(list)
    ordered = sorted(
        (ln for ln in lines if ln.pinned_file_id is not None),
        key=lambda ln: (ln.title.casefold(), ln.identity_id, ln.station),
    )
    for line in ordered:
        pinned = line.pinned_file_id
        if pinned is None:
            continue
        next_file, next_path = _next(line, master)
        pins[pinned].append(
            PinnedLine(
                identity_id=line.identity_id,
                station_id=line.station_id,
                station=line.station,
                title=line.title,
                artist=line.artist,
                plays=line.plays,
                next_file_id=next_file,
                next_path=next_path,
            )
        )
    return pins


def _artists(ordered: Sequence[PerformanceFacts]) -> tuple[CreditName, ...]:
    seen: set[str] = set()
    artists: list[CreditName] = []
    for performance in ordered:
        for part in performance.credit:
            if _artist_key(part) not in seen:
                seen.add(_artist_key(part))
                artists.append(CreditName(part.artist_id, part.name))
    return tuple(artists)


def _acts(
    ordered: Sequence[PerformanceFacts],
    default: UUID | None,
    lines: Sequence[WorkLine],
    copy_owner: Mapping[UUID, UUID],
) -> tuple[Act, ...]:
    keys: dict[frozenset[str], list[PerformanceFacts]] = {}
    for performance in ordered:
        if performance.credit:
            keys.setdefault(_act_key(performance), []).append(performance)
    if len(keys) < 2:
        return ()
    act_of = {p.id: key for key, members in keys.items() for p in members}
    default_key = act_of.get(default) if default is not None else None
    lines_of: dict[frozenset[str], set[UUID]] = defaultdict(set)
    reached: dict[str, set[UUID]] = defaultdict(set)
    for line in lines:
        if line.source is None:
            continue
        if line.source == PlaySource.PERFORMANCE:
            owner = copy_owner.get(line.file_id) if line.file_id is not None else None
            key = act_of.get(owner) if owner is not None else None
            if key is not None:
                lines_of[key].add(line.identity_id)
        elif default_key is not None:
            lines_of[default_key].add(line.identity_id)
            if line.station_format is not None:
                reached[line.station_format].add(line.identity_id)
    acts = [
        Act(
            credit=credit_text(members[0].credit),
            default=key == default_key,
            lines=len(lines_of[key]),
            reached={f: len(ids) for f, ids in sorted(reached.items())}
            if key == default_key
            else {},
        )
        for key, members in keys.items()
    ]
    return tuple(sorted(acts, key=lambda a: (not a.default, a.credit.casefold())))


def work_page(
    performances: Sequence[PerformanceFacts],
    master: MasterCopy | None,
    lines: Sequence[WorkLine],
) -> WorkPage:
    """The work page's performances, artists and acts (module docstring)."""
    copy_owner = {c: p.id for p in performances for c in p.copy_ids}
    default = copy_owner.get(master.file_id) if master is not None else None
    ordered = _order(performances, default)
    pins = _pins(lines, master)
    stored: dict[UUID, set[UUID]] = defaultdict(set)
    for line in lines:
        owner = copy_owner.get(line.file_id) if line.file_id is not None else None
        if line.source == PlaySource.PERFORMANCE and owner is not None:
            stored[owner].add(line.identity_id)
    views = tuple(
        PerformanceView(
            id=p.id,
            credit=p.credit,
            credit_text=credit_text(p.credit),
            version_type=p.version_type,
            default=p.id == default,
            preferred_copy_id=p.preferred_copy_id,
            lines=len(stored[p.id]),
            copies=tuple(CopyView(c, tuple(pins.get(c, ()))) for c in p.copy_ids),
        )
        for p in ordered
    )
    return WorkPage(views, _artists(ordered), _acts(ordered, default, lines, copy_owner))
