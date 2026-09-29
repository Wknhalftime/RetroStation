from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.domain.curation import SongMaster
from backend.domain.enums import FileStatus, SelectionMethod
from backend.domain.library import LibraryFile
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository

logger = structlog.get_logger()

# Scoring constants per spec Section 5.4
RELEASE_STATUS_SCORE: dict[str, int] = {"promotion": 100, "official": 0}
RELEASE_TYPE_SCORE: dict[str, int] = {
    "album": 80,
    "ep": 70,
    "single": 60,
    "compilation": 40,
    "live": 30,
    "other": 20,
}
FORMAT_BONUS: dict[str, int] = {"flac": 10, "aac": 6, "ogg": 6, "mp3": 3}


def score_file_row(row: dict[str, Any]) -> tuple[int, int, int]:
    """Return (score, bitrate, duration_ms) for a raw DB row dict.

    Used by async router helpers that work with psycopg row dicts rather than
    domain objects. Shares the same scoring constants as :func:`_score_file`.
    """
    score = 0
    rs = row.get("release_status")
    if rs:
        score += RELEASE_STATUS_SCORE.get(rs, 0)
    rt = row.get("release_type")
    if rt:
        score += RELEASE_TYPE_SCORE.get(rt, RELEASE_TYPE_SCORE["other"])
    fmt = (row.get("format") or "").lower()
    score += FORMAT_BONUS.get(fmt, 1)
    return score, row.get("bitrate") or 0, row.get("duration_ms") or 0


def _score_file(lib_file: LibraryFile) -> tuple[int, int, int]:
    """Return (score, bitrate, duration_ms) tuple for sorting.

    Higher is better for all three values.
    """
    score = 0
    if lib_file.audio.release_status is not None:
        score += RELEASE_STATUS_SCORE.get(lib_file.audio.release_status.value, 0)
    if lib_file.audio.release_type is not None:
        score += RELEASE_TYPE_SCORE.get(
            lib_file.audio.release_type.value, RELEASE_TYPE_SCORE["other"]
        )
    fmt = (lib_file.format or "").lower()
    score += FORMAT_BONUS.get(fmt, 1)

    bitrate = lib_file.audio.bitrate or 0
    duration = lib_file.audio.duration_ms or 0
    return score, bitrate, duration


def _present(files: list[LibraryFile]) -> list[LibraryFile]:
    """Files on disk: a missing file is never a song master."""
    return [f for f in files if f.file_status == FileStatus.PRESENT]


def recalculate_song_masters(
    work_ids: list[str],
    song_master_repo: SongMasterRepository,
    recording_repo: RecordingRepository | None = None,
    library_file_repo: LibraryFileRepository | None = None,
) -> None:
    """Recalculate song masters for the given work IDs.

    For each work_id:
    1. Skip if a manual selection already exists.
    2. Gather all recordings for the work.
    3. Gather all library files for each recording.
    4. Score each file and pick the best (tiebreak: bitrate, then duration_ms).
    5. Upsert the SongMaster.

    When recording_repo or library_file_repo is None, falls back to no-op
    (graceful degradation for callers that don't yet have those repos wired).
    """
    if not work_ids:
        return

    if recording_repo is None or library_file_repo is None:
        logger.info(
            "master_selection_recalculate_skipped",
            work_ids=len(work_ids),
            note="recording_repo or library_file_repo not provided",
        )
        return

    updated = 0
    skipped_manual = 0

    for work_id in work_ids:
        existing = song_master_repo.get_by_work(work_id)
        if existing is not None and existing.selection_method == SelectionMethod.MANUAL:
            skipped_manual += 1
            continue

        recordings = recording_repo.get_by_work(work_id)
        all_files: list[LibraryFile] = []
        for recording in recordings:
            all_files.extend(_present(library_file_repo.get_by_recording(recording.id)))

        if not all_files:
            logger.debug("master_selection_no_files", work_id=work_id)
            continue

        best = max(all_files, key=_score_file)
        score_val, _, _ = _score_file(best)

        master = SongMaster(
            id=existing.id if existing else uuid4(),
            work_id=work_id,
            preferred_file_id=best.id,
            selection_method=SelectionMethod.AUTO,
            score=score_val,
        )
        song_master_repo.upsert(master)
        updated += 1
        logger.debug("master_selection_updated", work_id=work_id, score=score_val)

    logger.info(
        "master_selection_recalculate_complete",
        work_ids=len(work_ids),
        updated=updated,
        skipped_manual=skipped_manual,
    )


@dataclass(frozen=True)
class _KeepMaster:
    """Leave the work's master as it is."""


@dataclass(frozen=True)
class _RemoveMaster:
    """The work has no present file and its master points outside it."""


@dataclass(frozen=True)
class _StoreMaster:
    """Store ``master``; ``over_manual`` when it replaces a manual pick no longer valid."""

    master: SongMaster
    over_manual: bool


_MasterDecision = _KeepMaster | _RemoveMaster | _StoreMaster


def _holds(master: SongMaster | None, files: list[LibraryFile]) -> bool:
    """Whether the master's file is one of ``files``."""
    return master is not None and any(f.id == master.preferred_file_id for f in files)


def _pick_from_files(
    work_id: str,
    present: list[LibraryFile],
    manual_file_id: UUID | None,
    master_id: UUID,
) -> SongMaster:
    """The carried manual choice when it is present, else the best-scoring present file."""
    carried = next((f for f in present if f.id == manual_file_id), None)
    chosen = carried if carried is not None else max(present, key=_score_file)
    method = SelectionMethod.MANUAL if carried is not None else SelectionMethod.AUTO
    return SongMaster(
        id=master_id,
        work_id=work_id,
        preferred_file_id=chosen.id,
        selection_method=method,
        score=_score_file(chosen)[0],
    )


def _decide_master(
    work_id: str,
    existing: SongMaster | None,
    work_files: list[LibraryFile],
    manual_file_id: UUID | None,
) -> _MasterDecision:
    """Decide a work's master from its files (any status) and its current master."""
    present = _present(work_files)
    is_manual = existing is not None and existing.selection_method == SelectionMethod.MANUAL
    if is_manual and _holds(existing, present):
        return _KeepMaster()
    if not present:
        # A2 keeps a master on the work's own file (it may come back); one that now
        # points outside the work cannot stay, and a missing file is never chosen.
        if existing is None or _holds(existing, work_files):
            return _KeepMaster()
        return _RemoveMaster()
    master_id = existing.id if existing is not None else uuid4()
    master = _pick_from_files(work_id, present, manual_file_id, master_id)
    return _StoreMaster(master=master, over_manual=is_manual)


def _apply_master_decision(
    work_id: str,
    decision: _MasterDecision,
    song_master_repo: SongMasterRepository,
) -> None:
    """Write the decision; only an invalid manual pick is replaced past upsert's guard."""
    if isinstance(decision, _RemoveMaster):
        song_master_repo.delete_by_work(work_id)
    elif isinstance(decision, _StoreMaster) and decision.over_manual:
        song_master_repo.replace(decision.master)
    elif isinstance(decision, _StoreMaster):
        song_master_repo.upsert(decision.master)


def reselect_master_from_files(
    work_id: str,
    song_master_repo: SongMasterRepository,
    library_file_repo: LibraryFileRepository,
    manual_file_id: UUID | None = None,
) -> None:
    """Pick a work's song master from the files attached to it directly.

    Unlike :func:`recalculate_song_masters` this reads ``library_files.work_id``
    rather than going through recordings, which most local works lack. A manual
    master is kept while its file is still a present file of the work; one
    whose file went missing or moved away is replaced by the automatic pick.
    With no present file, the master is kept while it points at a file of the
    work and removed otherwise. ``manual_file_id`` carries a manual choice over
    from a work merged into this one, if that file is attached.
    """
    existing = song_master_repo.get_by_work(work_id)
    work_files = library_file_repo.get_by_work(work_id)
    decision = _decide_master(work_id, existing, work_files, manual_file_id)
    _apply_master_decision(work_id, decision, song_master_repo)
