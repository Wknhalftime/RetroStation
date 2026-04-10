from __future__ import annotations

from uuid import uuid4

import structlog

from backend.domain.enums import SelectionMethod
from backend.domain.models import SongMaster
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository

logger = structlog.get_logger()

# Scoring constants per spec Section 5.4
RELEASE_STATUS_SCORE: dict[str, int] = {"promotion": 100, "official": 0}
RELEASE_TYPE_SCORE: dict[str, int] = {
    "album": 80, "ep": 70, "single": 60,
    "compilation": 40, "live": 30, "other": 20,
}
FORMAT_BONUS: dict[str, int] = {"flac": 10, "aac": 6, "ogg": 6, "mp3": 3}


def _score_file(lib_file: object) -> tuple[int, int, int]:
    """Return (score, bitrate, duration_ms) tuple for sorting.

    Higher is better for all three values.
    """
    from backend.domain.models import LibraryFile
    f: LibraryFile = lib_file  # type: ignore[assignment]

    score = 0
    if f.audio.release_status is not None:
        score += RELEASE_STATUS_SCORE.get(f.audio.release_status.value, 0)
    if f.audio.release_type is not None:
        score += RELEASE_TYPE_SCORE.get(f.audio.release_type.value, RELEASE_TYPE_SCORE["other"])
    fmt = (f.format or "").lower()
    score += FORMAT_BONUS.get(fmt, 1)

    bitrate = f.audio.bitrate or 0
    duration = f.audio.duration_ms or 0
    return score, bitrate, duration


def recalculate(
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
        all_files = []
        for recording in recordings:
            all_files.extend(library_file_repo.get_by_recording(recording.id))

        if not all_files:
            logger.debug("master_selection_no_files", work_id=work_id)
            continue

        # Sort: primary score DESC, then bitrate DESC, then duration_ms DESC
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
