"""Keep files' performances and preferred copies right (phase 3a PR 2; SPEC S2, S3, S5, E2, E3,
E8, D5, D6; design C40, C71; AUD-R075).

Every writer that sets a file's work keeps one order (S3):

1. :func:`leave`: before files leave their performances, re-pick each preferred copy that is
   one of them, and remember where they were;
2. the writer's own ``work_id`` write, which clears ``performance_id`` in the same statement;
3. :func:`assign_performance` for each file that now sits on a work;
4. :func:`settle`: the lines storing a performance the writer touched are handed off or
   released (E3, D5, question B), and an emptied performance is deleted. A writer that
   deletes the work a file left settles first, or the work's cascade would drop the lines'
   performances unlogged.

Everything here writes in the caller's transaction and commits nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from uuid import UUID

import structlog

from backend.domain.enums import FileStatus
from backend.domain.performances import (
    CopyRow,
    CopyScore,
    FileForPerformance,
    PerformanceKey,
    StoredLineOutcome,
    best_copy,
    credit_mbids,
    find_performance,
    fits,
    new_credits,
    stored_line_outcome,
)
from backend.repositories.performances import PerformanceRepository
from backend.services.master_selection_service import score_file_row
from backend.services.normalization import extract_version_info
from backend.services.song_match_service import SongMatchService

logger = structlog.get_logger()

PATH_BATCH = 1_000

type Left = dict[UUID, set[UUID]]
"""Each performance files left, with the files that left it."""


def file_key(file: FileForPerformance) -> PerformanceKey:
    """S2's key of a file: its recording MBID, credit artists, version and length."""
    _, version = extract_version_info(file.title or "")
    return PerformanceKey(
        recording_mbid=file.recording_mbid,
        credit_mbids=credit_mbids(file.credit),
        version_type=version,
        length_ms=file.length_ms,
    )


def assign_performance(file_id: UUID, performances: PerformanceRepository) -> UUID | None:
    """Find or create the file's performance in its current work (S2's rule); idempotent."""
    file = performances.file_for_performance(file_id)
    if file is None or file.work_id is None:
        return None
    if file.performance_id is not None:
        return file.performance_id
    key = file_key(file)
    found = find_performance(key, performances.candidates(file.work_id))
    if found is None:
        artists = performances.artists_by_mbid(sorted(key.credit_mbids))
        found = performances.create(file.work_id, key, new_credits(file.credit, artists))
    performances.hold(file_id, found)
    return found


def _scored(copies: Iterable[CopyRow]) -> list[CopyScore]:
    return [CopyScore(c.file_id, c.file_status, score_file_row(_columns(c))) for c in copies]


def _columns(copy: CopyRow) -> dict[str, object]:
    columns = asdict(copy)
    columns.pop("file_id")
    columns.pop("file_status")
    return columns


def repick(
    performance_id: UUID, performances: PerformanceRepository, *, leaving: set[UUID]
) -> None:
    """The best copy left in the performance, present before missing (S5)."""
    left = [c for c in performances.copies(performance_id) if c.file_id not in leaving]
    performances.set_preferred_copy(performance_id, best_copy(_scored(left)))


def leave(
    file_ids: Iterable[UUID],
    performances: PerformanceRepository,
    *,
    survivor: UUID | None = None,
) -> Left:
    """Before *file_ids* leave their performances: re-pick each preferred copy that is one of
    them, or re-point it to *survivor* (a fold) when the survivor is a copy of the same
    performance. Returns where the files were."""
    left: Left = {}
    for file_id in sorted(set(file_ids)):
        file = performances.file_for_performance(file_id)
        if file is not None and file.performance_id is not None:
            left.setdefault(file.performance_id, set()).add(file_id)
    for performance_id, leaving in left.items():
        if performances.preferred_copy(performance_id) not in leaving:
            continue
        members = {c.file_id for c in performances.copies(performance_id)}
        if survivor is not None and survivor in members and survivor not in leaving:
            performances.set_preferred_copy(performance_id, survivor)
        else:
            repick(performance_id, performances, leaving=leaving)
    return left


def assign_all(file_ids: Iterable[UUID], performances: PerformanceRepository) -> None:
    """:func:`assign_performance` for each file, in file-id order."""
    for file_id in sorted(set(file_ids)):
        assign_performance(file_id, performances)


def settle(
    left: Left,
    performances: PerformanceRepository,
    song_matches: SongMatchService,
    *,
    survivors: Mapping[UUID, UUID] | None = None,
) -> None:
    """E3, D5, question B: the lines storing each performance in *left*, then the emptied
    performances. A folded file sits where its survivor (in *survivors*) does; a deleted file
    sits nowhere."""
    for performance_id, files in sorted(left.items()):
        work_id = performances.work_of(performance_id)
        if work_id is None:
            continue
        emptied = performances.file_count(performance_id) == 0
        landing = _landing(files, performances, survivors or {})
        handed: dict[UUID, list[UUID]] = {}
        released: list[UUID] = []
        for line in performances.stored_lines(performance_id):
            outcome, target = stored_line_outcome(
                line_work_id=line.line_work_id,
                performance_work_id=work_id,
                emptied=emptied,
                landing=landing,
            )
            if outcome == StoredLineOutcome.HANDED_OFF and target is not None:
                handed.setdefault(target, []).append(line.match_id)
            elif outcome == StoredLineOutcome.RELEASED:
                released.append(line.match_id)
        for target, match_ids in handed.items():
            song_matches.hand_off_performance(match_ids, target)
            if performances.preferred_copy(target) is None:
                repick(target, performances, leaving=set())
            logger.info(
                "stored_performance_handed_off",
                from_performance_id=str(performance_id),
                to_performance_id=str(target),
                lines=len(match_ids),
            )
        if released:
            song_matches.release_performance(released)
            logger.info(
                "stored_performance_released",
                performance_id=str(performance_id),
                lines=len(released),
            )
        if emptied:
            performances.delete(performance_id)


def _landing(
    files: set[UUID], performances: PerformanceRepository, survivors: Mapping[UUID, UUID]
) -> frozenset[tuple[UUID, str]]:
    landed: set[tuple[UUID, str]] = set()
    for file_id in files:
        file = performances.file_for_performance(survivors.get(file_id, file_id))
        if file is None or file.performance_id is None or file.work_id is None:
            continue
        landed.add((file.performance_id, file.work_id))
    return frozenset(landed)


def move_out(
    file_ids: Iterable[UUID],
    performances: PerformanceRepository | None,
) -> Left:
    """:func:`leave`, or nothing when the writer runs without the port (the locked bundles)."""
    return {} if performances is None else leave(file_ids, performances)


def place(
    file_ids: Iterable[UUID],
    left: Left,
    performances: PerformanceRepository | None,
    song_matches: SongMatchService,
) -> None:
    """Steps 3 and 4 after a writer's move: assign, then settle."""
    if performances is None:
        return
    assign_all(file_ids, performances)
    settle(left, performances, song_matches)


def reassign_retagged(
    file_paths: Iterable[str], performances: PerformanceRepository, song_matches: SongMatchService
) -> int:
    """D6, tags only: a file at one of *file_paths* whose tags no longer fit the performance it
    holds is placed again in its work. One read per ``PATH_BATCH`` paths; returns how many
    moved."""
    paths = sorted(set(file_paths))
    stale: list[UUID] = []
    for start in range(0, len(paths), PATH_BATCH):
        for file, held in performances.held_files(paths[start : start + PATH_BATCH]):
            if not fits(file_key(file), held):
                stale.append(file.id)
    if not stale:
        return 0
    left = leave(stale, performances)
    for file_id in stale:
        performances.hold(file_id, None)
    assign_all(stale, performances)
    settle(left, performances, song_matches)
    return len(stale)


def prefer_chosen(file_id: UUID, performances: PerformanceRepository) -> None:
    """Set master: the chosen file becomes its performance's preferred copy, when that
    performance has one (S5)."""
    file = performances.file_for_performance(file_id)
    if file is None or file.performance_id is None:
        return
    if performances.preferred_copy(file.performance_id) is not None:
        performances.set_preferred_copy(file.performance_id, file_id)


def repick_stranded(performances: PerformanceRepository) -> int:
    """E8: each preferred copy gone missing is re-picked among its performance's present
    copies; with none present it stays. Returns how many were re-picked."""
    repicked = 0
    for performance_id in performances.stranded_preferred_copies():
        present = [
            c for c in performances.copies(performance_id) if c.file_status == FileStatus.PRESENT
        ]
        best = best_copy(_scored(present))
        if best is not None:
            performances.set_preferred_copy(performance_id, best)
            repicked += 1
    return repicked
