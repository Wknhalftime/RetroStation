"""Fold library rows whose file went missing into the present copy of the same track.

A file moved and retagged in one go (MusicBrainz Picard does both) changes
its content, so the scan cannot tell it was moved: it indexes the new path
as a new row and marks the old one missing, which still holds the file's
matches and song-master pick. For each missing row this finds the one
present row holding the same track, its successor, the way Navidrome pairs
missing tracks by persistent ID, with the audio fingerprint as the first,
exact rule: an audio match wins outright once every hashable candidate has
its fingerprint, and until then the pairing waits for a later run. Rows
with no successor, or several equally good ones, are left alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from uuid import UUID

from backend.domain.enums import FileStatus
from backend.domain.library import (
    AUDIO_HASHABLE_FORMATS,
    LibraryFile,
    MissingFileMove,
    MissingFilePlan,
)
from backend.repositories.format_overrides import FormatOverrideRepository
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


def _same_audio(a: LibraryFile, b: LibraryFile) -> bool:
    return a.audio_hash is not None and a.audio_hash == b.audio_hash


def is_same_track(missing: LibraryFile, candidate: LibraryFile) -> bool:
    """Whether *candidate* holds the track *missing* held.

    The same audio fingerprint, whatever the tags now say. Otherwise, with
    durations within 2 s: the same recording on the same release, or the
    same artist's track number and title on the same release.
    """
    if _same_audio(missing, candidate):
        return True
    if not _durations_agree(missing, candidate):
        return False
    if _same_recording(missing, candidate):
        return True
    key = _release_track(missing)
    return key is not None and key == _release_track(candidate)


def _audio_twins(missing: LibraryFile, file_repo: LibraryFileRepository) -> list[LibraryFile]:
    if missing.audio_hash is None:
        return []
    return file_repo.get_by_audio_hash(missing.audio_hash)


def _recording_twins(missing: LibraryFile, file_repo: LibraryFileRepository) -> list[LibraryFile]:
    mbid = missing.audio.recording_mbid
    return file_repo.get_by_recording_mbid(mbid) if mbid is not None else []


def _release_track_twins(
    missing: LibraryFile,
    file_repo: LibraryFileRepository,
) -> list[LibraryFile]:
    a = missing.audio
    if (
        a.normalized_artist_name is None
        or a.release_title is None
        or a.track_number is None
        or a.normalized_title is None
    ):
        return []
    return file_repo.get_present_by_track(
        a.normalized_artist_name, a.release_title, a.track_number, a.normalized_title
    )


def successor_candidates(
    missing: LibraryFile,
    file_repo: LibraryFileRepository,
) -> list[LibraryFile]:
    """Rows that may hold *missing*'s track; ready_successors decides."""
    found = {
        f.id: f
        for twins in (_audio_twins, _recording_twins, _release_track_twins)
        for f in twins(missing, file_repo)
    }
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


def ready_successors(missing: LibraryFile, candidates: list[LibraryFile]) -> list[LibraryFile]:
    """Candidates that can take over *missing* now: PRESENT, grouped, the same track."""
    return [
        c
        for c in candidates
        if c.id != missing.id
        and c.file_status == FileStatus.PRESENT
        and c.work_id is not None
        and is_same_track(missing, c)
    ]


def _awaiting_fingerprint(candidate: LibraryFile) -> bool:
    """Whether the hash backfill will still give *candidate* an audio fingerprint."""
    return candidate.audio_hash is None and candidate.format in AUDIO_HASHABLE_FORMATS


def choose_successor(missing: LibraryFile, ready: list[LibraryFile]) -> LibraryFile | None:
    """The successor among *ready*, or None to leave *missing* alone for now.

    An audio match wins outright once every hashable candidate has its
    fingerprint; until then None, as an unhashed candidate may be the true
    successor. Ties, and the other rules, settle by an identical file name.
    """
    audio = [c for c in ready if _same_audio(missing, c)]
    if audio and any(_awaiting_fingerprint(c) for c in ready):
        return None
    return _pick(missing, audio or ready)


def _move(missing: LibraryFile, successor: LibraryFile) -> MissingFileMove:
    return MissingFileMove(
        missing_id=missing.id,
        missing_path=missing.file_path,
        missing_work_id=missing.work_id,
        successor_id=successor.id,
        successor_path=successor.file_path,
        successor_work_id=successor.work_id,
    )


def plan_missing_file_moves(
    missing_rows: list[LibraryFile],
    candidates_for: Callable[[LibraryFile], list[LibraryFile]],
) -> MissingFilePlan:
    """Pair each missing row with its successor. Reads only.

    A successor can take over only one missing row per run: the first by
    path wins, and a later row whose pick is already claimed is ambiguous.
    """
    moves: list[MissingFileMove] = []
    ambiguous: list[str] = []
    unmatched: list[str] = []
    claimed: set[UUID] = set()
    for missing in sorted(missing_rows, key=lambda f: f.file_path):
        ready = ready_successors(missing, candidates_for(missing))
        successor = choose_successor(missing, ready) if ready else None
        if not ready:
            unmatched.append(missing.file_path)
        elif successor is None or successor.id in claimed:
            ambiguous.append(missing.file_path)
        else:
            claimed.add(successor.id)
            moves.append(_move(missing, successor))
    return MissingFilePlan(tuple(moves), tuple(ambiguous), tuple(unmatched))


@dataclass(frozen=True)
class ReconciliationRepos:
    """The repositories a fold writes through."""

    files: LibraryFileRepository
    matches: MatchRepository
    works: WorkRepository
    song_masters: SongMasterRepository
    format_overrides: FormatOverrideRepository


def _has_status(file_repo: LibraryFileRepository, file_id: UUID, status: FileStatus) -> bool:
    row = file_repo.get_by_id(file_id)
    return row is not None and row.file_status == status


def _still_foldable(move: MissingFileMove, file_repo: LibraryFileRepository) -> bool:
    """Whether the missing row is still missing and its successor still present."""
    return _has_status(file_repo, move.missing_id, FileStatus.MISSING) and _has_status(
        file_repo, move.successor_id, FileStatus.PRESENT
    )


def apply_missing_file_move(move: MissingFileMove, repos: ReconciliationRepos) -> bool:
    """Move every reference to the missing row onto its successor, then delete it.

    When the successor sits in another work, the moved matches and format
    overrides take that work, and the work the missing row left re-picks its
    master from its own present files and is deleted once nothing references
    it. A move the library has overtaken since planning (the missing row came
    back, or the successor went missing) is skipped. Returns whether it folded.
    """
    if not _still_foldable(move, repos.files):
        return False
    repos.files.merge_into(move.missing_id, move.successor_id)
    if not move.crosses_work:
        return True
    repos.matches.move_to_work(move.successor_id, move.successor_work_id)
    if move.missing_work_id is not None and move.successor_work_id is not None:
        repos.format_overrides.move_to_work(
            move.successor_id, move.missing_work_id, move.successor_work_id
        )
    if move.missing_work_id is not None:
        reselect_master_from_files(move.missing_work_id, repos.song_masters, repos.files)
        repos.works.delete_if_empty(move.missing_work_id)
    return True


def repick_stranded_masters(repos: ReconciliationRepos) -> int:
    """Re-pick every AUTO master left on a missing file whose work has a present one.

    Manual masters are the user's and are left alone. Returns how many works
    were re-picked.
    """
    work_ids = repos.song_masters.list_work_ids_with_missing_master()
    for work_id in work_ids:
        reselect_master_from_files(work_id, repos.song_masters, repos.files)
    return len(work_ids)


def plan_for_library(file_repo: LibraryFileRepository) -> MissingFilePlan:
    """Plan reconciliation for every missing row in the library."""
    return plan_missing_file_moves(
        file_repo.get_missing(),
        lambda m: successor_candidates(m, file_repo),
    )
