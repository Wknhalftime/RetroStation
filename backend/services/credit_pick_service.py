"""Credit picks: propose, list, apply and pick (phase 3a PR 3; SPEC S5, S8, S9, E6, D4, D8, D9;
design C57, C64, C65, C71, M28; AUD-R039, AUD-R076).

A run proposes, for every matched line whose log credit ``choose_performance`` answers with a
non-default performance, one row; it keeps decided rows, keeps an undecided row only while its
answer is unchanged, and replaces a changed one with a new row, so an id Lance saw always means
the answer he saw (C64). Nothing it does plays differently. Apply stores the ``credit`` rows Lance
saw minus his skips, and a version-line pick stores at once; both go through
``SongMatchService`` (C52) and give the stored performance its preferred copy first (S5). A row
stale by the time Lance acts is never applied, and the page leaves stale rows out.

Everything here writes in the caller's transaction and commits nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog

from backend.domain.credit_picks import (
    CandidateNotListedError,
    LineNow,
    Proposal,
    ProposalAnswer,
    ProposalKind,
    ProposalKindError,
    ProposalNotFoundError,
    ProposalStaleError,
    ProposalState,
    ProposalTwiceError,
    is_stale,
    live_candidates,
    propose,
    still_valid,
)
from backend.repositories.credit_picks import CreditPickRepository, FileShown, LineShown
from backend.repositories.matches import MatchRepository
from backend.repositories.performances import PerformanceRepository
from backend.services.performance_service import copy_to_prefer, give_preferred_copy
from backend.services.song_match_service import SongMatchService

logger = structlog.get_logger()


@dataclass(frozen=True)
class CreditPickRepos:
    credit_picks: CreditPickRepository
    performances: PerformanceRepository
    matches: MatchRepository


@dataclass(frozen=True)
class FileView:
    file_id: UUID
    file_path: str
    track_title: str | None
    file_status: str
    source: str | None = None


@dataclass(frozen=True)
class CandidateView:
    performance_id: UUID
    credit_names: tuple[str, ...]
    would_play: FileView | None


@dataclass(frozen=True)
class ProposalView:
    id: UUID
    identity_id: UUID
    kind: ProposalKind
    reason: str
    state: ProposalState
    title: str
    artist_name: str
    plays: int
    work_id: str | None
    work_title: str | None
    performance_id: UUID | None
    plays_now: FileView | None
    would_play: FileView | None
    candidates: tuple[CandidateView, ...]


@dataclass(frozen=True)
class CreditPicksPage:
    credit: tuple[ProposalView, ...]
    version_lines: tuple[ProposalView, ...]
    total: int


@dataclass(frozen=True)
class ApplyResult:
    applied: int
    skipped: int
    stale: tuple[UUID, ...]


# --- proposing ------------------------------------------------------------------------------------


def propose_all(picks: CreditPickRepository) -> int:
    """Propose for every matched line (S8, D4); how many rows wait for Lance afterwards."""
    lines = picks.lines_to_propose()
    works = picks.work_credits(sorted({line.work_id for line in lines}))
    artists = {a for line in lines for a in line.artists}
    for credits in works.values():
        artists.update(a for p in credits.performances for a in p.artists)
        if credits.artist_id is not None:
            artists.add(credits.artist_id)
    primary_of = picks.primaries(artists)
    wanted: dict[UUID, ProposalAnswer] = {}
    for line in lines:
        work = works.get(line.work_id)
        answer = None if work is None else propose(line, work, primary_of)
        if answer is not None:
            wanted[line.identity_id] = answer
    existing = {p.identity_id: p for p in picks.all_proposals()}
    undecided = {i: p for i, p in existing.items() if p.state == ProposalState.PROPOSED}
    picks.delete([p.id for i, p in undecided.items() if wanted.get(i) != p.answer])
    for identity_id, answer in wanted.items():
        current = existing.get(identity_id)
        if current is None or (
            current.state == ProposalState.PROPOSED and current.answer != answer
        ):
            picks.insert(identity_id, answer)
    waiting = sum(1 for i in wanted if existing.get(i) is None or i in undecided)
    logger.info("credit_picks_proposed", lines=len(lines), waiting=waiting)
    return waiting


# --- reading --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fresh:
    proposal: Proposal
    now: LineNow
    candidates: tuple[UUID, ...]


def _fresh(rows: Sequence[Proposal], picks: CreditPickRepository) -> list[_Fresh]:
    """The rows not stale now, each with its line as it stands and its live candidates."""
    lines = picks.lines_now([r.identity_id for r in rows])
    works = _works_of(rows, picks)
    fresh: list[_Fresh] = []
    for row in rows:
        now = lines.get(row.identity_id)
        if now is not None and not is_stale(row, now, works):
            fresh.append(_Fresh(row, now, live_candidates(row, now, works)))
    return fresh


def _works_of(rows: Sequence[Proposal], picks: CreditPickRepository) -> dict[UUID, str]:
    performance_ids = {c for r in rows for c in r.candidates}
    performance_ids.update(r.performance_id for r in rows if r.performance_id is not None)
    return picks.performance_works(performance_ids)


def count(picks: CreditPickRepository) -> int:
    """The undecided rows not stale now: Library Status's count (S9, D7)."""
    waiting = [r for r in picks.listed() if r.state == ProposalState.PROPOSED]
    return len(_fresh(waiting, picks))


def page(repos: CreditPickRepos) -> CreditPicksPage:
    """Both lists, stale rows left out, by plays, most first, then by line id (S9)."""
    fresh = _fresh(repos.credit_picks.listed(), repos.credit_picks)
    shown = repos.credit_picks.lines_shown([f.proposal.identity_id for f in fresh])
    playing = {
        p: copy_to_prefer(p, repos.performances)
        for f in fresh
        for p in {*f.candidates, *([f.proposal.performance_id] if _applies(f) else [])}
        if p is not None
    }
    files = repos.credit_picks.files_shown(
        {*(f for f in playing.values() if f is not None)}
        | {s.plays_now_file_id for s in shown.values() if s.plays_now_file_id is not None}
    )
    names = repos.credit_picks.credit_names({c for f in fresh for c in f.candidates})
    views = [
        _view(f, shown[f.proposal.identity_id], playing, files, names)
        for f in fresh
        if f.proposal.identity_id in shown
    ]
    views.sort(key=lambda v: (-v.plays, str(v.identity_id)))
    return CreditPicksPage(
        credit=tuple(v for v in views if v.kind == ProposalKind.CREDIT),
        version_lines=tuple(v for v in views if v.kind != ProposalKind.CREDIT),
        total=sum(1 for v in views if v.state == ProposalState.PROPOSED),
    )


def _applies(fresh: _Fresh) -> bool:
    return fresh.proposal.kind == ProposalKind.CREDIT


def _file(
    file_id: UUID | None, files: Mapping[UUID, FileShown], source: str | None = None
) -> FileView | None:
    shown = None if file_id is None else files.get(file_id)
    if shown is None:
        return None
    return FileView(shown.file_id, shown.file_path, shown.track_title, shown.file_status, source)


def _view(
    fresh: _Fresh,
    shown: LineShown,
    playing: Mapping[UUID, UUID | None],
    files: Mapping[UUID, FileShown],
    names: Mapping[UUID, list[str]],
) -> ProposalView:
    row = fresh.proposal
    would = row.performance_id if _applies(fresh) else None
    return ProposalView(
        id=row.id,
        identity_id=row.identity_id,
        kind=row.kind,
        reason=row.reason.value,
        state=row.state,
        title=shown.title,
        artist_name=shown.artist_name,
        plays=shown.plays,
        work_id=shown.work_id,
        work_title=shown.work_title,
        performance_id=row.performance_id,
        plays_now=_file(shown.plays_now_file_id, files, shown.plays_now_source),
        would_play=None if would is None else _file(playing.get(would), files),
        candidates=tuple(
            CandidateView(c, tuple(names.get(c, [])), _file(playing.get(c), files))
            for c in fresh.candidates
        ),
    )


# --- deciding -------------------------------------------------------------------------------------


def apply(
    apply_ids: Sequence[UUID], skip_ids: Sequence[UUID], repos: CreditPickRepos
) -> ApplyResult:
    """Apply what Lance saw minus his skips, and record the skips (S9, C64, E6)."""
    if set(apply_ids) & set(skip_ids):
        raise ProposalTwiceError("an id was sent both to apply and to skip")
    rows = repos.credit_picks.proposals(list(dict.fromkeys([*apply_ids, *skip_ids])))
    for listed in rows.values():
        if listed.kind != ProposalKind.CREDIT:
            raise ProposalKindError(
                f"proposal {listed.id} is a {listed.kind.value}: pick it instead"
            )
    lines = repos.credit_picks.lines_now([r.identity_id for r in rows.values()])
    works = _works_of(list(rows.values()), repos.credit_picks)
    song_matches = SongMatchService(repos.matches)
    applied: list[UUID] = []
    stale = [i for i in [*apply_ids, *skip_ids] if i not in rows]
    for proposal_id in apply_ids:
        row = rows.get(proposal_id)
        if row is None or row.state != ProposalState.PROPOSED:
            continue
        if is_stale(row, lines.get(row.identity_id), works):
            stale.append(proposal_id)
            continue
        _store(row, row.performance_id, repos, song_matches)
        applied.append(proposal_id)
    skipped = [i for i in skip_ids if i in rows and rows[i].state == ProposalState.PROPOSED]
    repos.credit_picks.decide(applied, ProposalState.APPLIED)
    repos.credit_picks.decide(skipped, ProposalState.SKIPPED)
    logger.info(
        "credit_picks_applied", applied=len(applied), skipped=len(skipped), stale=len(stale)
    )
    return ApplyResult(len(applied), len(skipped), tuple(stale))


def _store(
    row: Proposal,
    performance_id: UUID | None,
    repos: CreditPickRepos,
    song_matches: SongMatchService,
) -> None:
    """Store *performance_id* (or none) on the row's match, its preferred copy given first."""
    if row.match_id is None:
        raise ProposalStaleError(f"proposal {row.id}'s match is gone")
    if performance_id is not None:
        give_preferred_copy(performance_id, repos.performances)
    song_matches.store_performance(row.match_id, performance_id)


def pick(proposal_id: UUID, performance_id: UUID | None, repos: CreditPickRepos) -> None:
    """A version line's or tie's pick, stored at once: a candidate, or none (S9, C65)."""
    row = repos.credit_picks.proposals([proposal_id]).get(proposal_id)
    if row is None:
        raise ProposalNotFoundError(f"no proposal {proposal_id}")
    if row.kind == ProposalKind.CREDIT:
        raise ProposalKindError(f"proposal {proposal_id} is applied, not picked")
    if performance_id is not None and performance_id not in row.candidates:
        raise CandidateNotListedError(f"performance {performance_id} isn't a candidate")
    now = repos.credit_picks.lines_now([row.identity_id]).get(row.identity_id)
    if now is None or now.match_id is None or now.match_id != row.match_id:
        raise ProposalStaleError(f"the line of proposal {proposal_id} has been re-resolved")
    if performance_id is not None:
        works = repos.credit_picks.performance_works([performance_id])
        if not still_valid(performance_id, now, works):
            raise ProposalStaleError(f"performance {performance_id} is gone or now the default")
    _store(row, performance_id, repos, SongMatchService(repos.matches))
    repos.credit_picks.decide([row.id], ProposalState.PICKED, performance_id)
    logger.info("credit_pick_picked", proposal_id=str(row.id), performance_id=str(performance_id))


def apply_on_copy(skip_lines: set[UUID], repos: CreditPickRepos) -> ApplyResult:
    """``apply_credit_picks.py``: every undecided ``credit`` row, but the lines skipped."""
    rows = [
        p
        for p in repos.credit_picks.all_proposals()
        if p.state == ProposalState.PROPOSED and p.kind == ProposalKind.CREDIT
    ]
    rows.sort(key=lambda p: str(p.id))
    skipped = [p.id for p in rows if p.identity_id in skip_lines]
    return apply([p.id for p in rows if p.identity_id not in skip_lines], skipped, repos)
