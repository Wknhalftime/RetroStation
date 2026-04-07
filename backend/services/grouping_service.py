"""Local-first song grouping -- 4-step matching algorithm."""
from __future__ import annotations

import logging
from uuid import uuid4

from rapidfuzz import fuzz

from backend.domain.enums import SelectionMethod
from backend.domain.models import LibraryFile, SongMaster
from backend.repositories.artists import ArtistRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.normalization import (
    normalize_artist,
    normalize_title,
    strict_normalize,
)

logger = logging.getLogger(__name__)


def _dynamic_threshold(title_length: int) -> float:
    """Return fuzzy-match threshold based on normalized title length."""
    if title_length < 5:
        return 95.0
    if title_length < 10:
        return 90.0
    if title_length <= 25:
        return 85.0
    return 80.0


def assign_work(
    file: LibraryFile,
    *,
    artist_repo: ArtistRepository,
    work_repo: WorkRepository,
    library_file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    song_master_repo: SongMasterRepository,
) -> str | None:
    """Assign a work_id to the given LibraryFile. Returns work_id or None."""
    raw_artist = file.artist_name or ""
    raw_title = file.track_title or ""
    if not raw_artist.strip() or not raw_title.strip():
        return None

    # Step 1: Hash shortcut
    existing_by_hash = library_file_repo.get_by_hash(file.file_hash)
    for existing in existing_by_hash:
        if existing.work_id is not None and existing.id != file.id:
            return existing.work_id

    # Step 2: MBID shortcut
    if file.recording_mbid:
        recording = recording_repo.get_by_id(file.recording_mbid)
        if recording and recording.work_id:
            return recording.work_id

    norm_artist = normalize_artist(raw_artist)
    norm_title = normalize_title(raw_title)

    # Step 3: Artist-first fuzzy match
    if norm_title:
        work_candidates = library_file_repo.get_candidates_by_artist(
            norm_artist, limit=100,
        )
        if work_candidates:
            threshold = _dynamic_threshold(len(norm_title))
            strict_input = strict_normalize(norm_title)

            best_work_id: str | None = None
            best_score = -1.0

            for work_id, sample_title in work_candidates:
                if strict_input == strict_normalize(sample_title):
                    score = 100.0
                else:
                    full = fuzz.ratio(norm_title, sample_title)
                    partial = fuzz.partial_ratio(norm_title, sample_title)
                    score = 0.7 * full + 0.3 * partial

                if score > best_score or (
                    score == best_score
                    and (best_work_id is None or work_id < best_work_id)
                ):
                    best_score = score
                    best_work_id = work_id

            if best_score >= threshold and best_work_id is not None:
                return best_work_id

    # Step 4: Create local work
    artist_id = artist_repo.upsert_local(raw_artist, norm_artist)
    work_id = work_repo.create_local(raw_title, artist_id)

    song_master_repo.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file.id,
            selection_method=SelectionMethod.AUTO,
        ),
    )
    return work_id
