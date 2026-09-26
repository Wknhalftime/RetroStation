"""Fold library rows whose file went missing into the present copy of the same track.

A file moved and retagged in one go (MusicBrainz Picard does both) changes
its content, so the scan cannot tell it was moved: it indexes the new path
as a new row and marks the old one missing, which still holds the file's
matches and song-master pick. For each missing row this finds the one
present row holding the same track, its successor, the way Navidrome pairs
missing tracks by persistent ID. Rows with no successor, or several equally
good ones, are left alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from uuid import UUID

from backend.domain.enums import FileStatus
from backend.domain.library import (
    LibraryFile,
    MissingFileMove,
    MissingFilePlan,
    MissingFileReconciliation,
)
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.master_selection_service import reselect_master_from_files

# A remaster or an edit on the same release differs by more than this.
MAX_DURATION_DIFF_MS = 2_000


def _durations_agree(a: LibraryFile, b: LibraryFile) -> bool:
    da, db = a.audio.duration_ms, b.audio.duration_ms
    return da is not None and db is not None and abs(da - db) <= MAX_DURATION_DIFF_MS


def _same_recording(a: LibraryFile, b: LibraryFile) -> bool:
    return (
        a.audio.recording_mbid is not None
        and a.audio.recording_mbid == b.audio.recording_mbid
        and a.audio.release_mbid == b.audio.release_mbid
    )


def _release_track(f: LibraryFile) -> tuple[object, ...] | None:
    a = f.audio
    if (
        a.release_title is None
        or a.track_number is None
        or a.normalized_title is None
        or a.normalized_artist_name is None
    ):
        return None
    return (
        a.normalized_artist_name,
        a.release_title,
        a.disc_number,
        a.track_number,
        a.normalized_title,
    )


def is_same_track(missing: LibraryFile, candidate: LibraryFile) -> bool:
    """Whether *candidate* holds the track *missing* held.

    Same recording on the same release, or the same artist's track number
    and title on the same release; either way with durations within 2 s.
    """
    if not _durations_agree(missing, candidate):
        return False
    if _same_recording(missing, candidate):
        return True
    key = _release_track(missing)
    return key is not None and key == _release_track(candidate)


def successor_candidates(
    missing: LibraryFile,
    file_repo: LibraryFileRepository,
) -> list[LibraryFile]:
    """PRESENT rows that may hold *missing*'s track; is_same_track decides."""
    found: dict[UUID, LibraryFile] = {}
    a = missing.audio
    if a.recording_mbid is not None:
        for f in file_repo.get_by_recording_mbid(a.recording_mbid):
            found[f.id] = f
    if (
        a.normalized_artist_name is not None
        and a.release_title is not None
        and a.track_number is not None
        and a.normalized_title is not None
    ):
        for f in file_repo.get_present_by_track(
            a.normalized_artist_name,
            a.release_title,
            a.track_number,
            a.normalized_title,
        ):
            found[f.id] = f
    return list(found.values())


def _file_name(path: str) -> str:
    return PureWindowsPath(path).name


def _pick(missing: LibraryFile, candidates: list[LibraryFile]) -> LibraryFile | None:
    """The only candidate, else the only one with *missing*'s file name, else None."""
    if len(candidates) == 1:
        return candidates[0]
    name = _file_name(missing.file_path)
    same_name = [c for c in candidates if _file_name(c.file_path) == name]
    return same_name[0] if len(same_name) == 1 else None


def plan_missing_file_moves(
    missing_rows: list[LibraryFile],
    candidates_for: Callable[[LibraryFile], list[LibraryFile]],
) -> MissingFilePlan:
    """Pair each missing row with its successor. Reads only.

    A successor must be PRESENT and already grouped (have a work), and can
    take over only one missing row per run: the first by path wins.
    """
    moves: list[MissingFileMove] = []
    ambiguous: list[str] = []
    unmatched: list[str] = []
    claimed: set[UUID] = set()
    for missing in sorted(missing_rows, key=lambda f: f.file_path):
        ready = [
            c
            for c in candidates_for(missing)
            if c.id != missing.id
            and c.file_status == FileStatus.PRESENT
            and c.work_id is not None
            and is_same_track(missing, c)
        ]
        if not ready:
            unmatched.append(missing.file_path)
            continue
        successor = _pick(missing, [c for c in ready if c.id not in claimed])
        if successor is None:
            ambiguous.append(missing.file_path)
            continue
        claimed.add(successor.id)
        moves.append(
            MissingFileMove(
                missing_id=missing.id,
                missing_path=missing.file_path,
                missing_work_id=missing.work_id,
                successor_id=successor.id,
                successor_path=successor.file_path,
                successor_work_id=successor.work_id,
            )
        )
    return MissingFilePlan(tuple(moves), tuple(ambiguous), tuple(unmatched))


@dataclass(frozen=True)
class ReconciliationRepos:
    """The repositories a fold writes through."""

    files: LibraryFileRepository
    matches: MatchRepository
    works: WorkRepository
    song_masters: SongMasterRepository


def apply_missing_file_move(move: MissingFileMove, repos: ReconciliationRepos) -> None:
    """Move every reference to the missing row onto its successor, then delete it.

    When the successor sits in another work, the moved matches take that
    work, and the work the missing row left re-picks its master from its
    own present files and is deleted once nothing references it.
    """
    repos.files.merge_into(move.missing_id, move.successor_id)
    if not move.crosses_work:
        return
    repos.matches.move_to_work(move.successor_id, move.successor_work_id)
    if move.missing_work_id is None:
        return
    reselect_master_from_files(move.missing_work_id, repos.song_masters, repos.files)
    repos.works.delete_if_empty(move.missing_work_id)


def plan_for_library(file_repo: LibraryFileRepository) -> MissingFilePlan:
    """Plan reconciliation for every missing row in the library."""
    return plan_missing_file_moves(
        file_repo.get_missing(),
        lambda m: successor_candidates(m, file_repo),
    )


def reconcile_missing_files(repos: ReconciliationRepos) -> MissingFileReconciliation:
    """Fold every missing row that has a successor into it. The caller commits."""
    plan = plan_for_library(repos.files)
    for move in plan.moves:
        apply_missing_file_move(move, repos)
    return MissingFileReconciliation(
        reconciled=len(plan.moves),
        ambiguous=len(plan.ambiguous),
        unmatched=len(plan.unmatched),
    )
