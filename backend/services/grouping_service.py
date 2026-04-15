"""Local-first song grouping -- 4-step matching algorithm with version dedup."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from rapidfuzz import fuzz

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import LibraryFile
from backend.repositories.artists import ArtistRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.normalization import (
    extract_version_info,
    normalize_artist,
    normalize_title,
    strict_normalize,
)

logger = logging.getLogger(__name__)


@dataclass
class GroupingResult:
    """Return type for assign_work — carries both work and recording IDs."""

    work_id: str
    recording_id: str | None = None


def _dynamic_threshold(title_length: int) -> float:
    """Return fuzzy-match threshold based on normalized title length."""
    if title_length < 5:
        return 95.0
    if title_length < 10:
        return 90.0
    if title_length <= 25:
        return 85.0
    return 80.0


def _try_hash_shortcut(
    file: LibraryFile,
    library_file_repo: LibraryFileRepository,
) -> GroupingResult | None:
    """Return existing GroupingResult if a file with the same hash already has a work_id."""
    for existing in library_file_repo.get_by_hash(file.file_hash):
        if existing.work_id is not None and existing.id != file.id:
            return GroupingResult(
                work_id=existing.work_id,
                recording_id=existing.recording_id,
            )
    return None


def _try_mbid_shortcut(
    file: LibraryFile,
    recording_repo: RecordingRepository,
) -> GroupingResult | None:
    """Return existing GroupingResult if recording MBID resolves to a known work."""
    if not file.audio.recording_mbid:
        return None
    recording = recording_repo.get_by_id(file.audio.recording_mbid)
    if recording and recording.work_id:
        return GroupingResult(
            work_id=recording.work_id,
            recording_id=recording.id,
        )
    return None


def _fuzzy_match_work(
    norm_base: str,
    norm_artist: str,
    work_repo: WorkRepository,
) -> str | None:
    """Fuzzy-match norm_base against work titles for norm_artist; return work_id or None."""
    if not norm_base:
        return None
    work_candidates = work_repo.get_candidates_by_normalized_artist(norm_artist, limit=100)
    if not work_candidates:
        return None

    threshold = _dynamic_threshold(len(norm_base))
    strict_input = strict_normalize(norm_base)
    best_work_id: str | None = None
    best_score = -1.0

    for candidate_id, candidate_title in work_candidates:
        norm_candidate = normalize_title(candidate_title)
        if strict_input == strict_normalize(norm_candidate):
            score = 100.0
        else:
            full_ratio_score = fuzz.ratio(norm_base, norm_candidate)
            partial_ratio_score = fuzz.partial_ratio(norm_base, norm_candidate)
            score = 0.7 * full_ratio_score + 0.3 * partial_ratio_score

        if score > best_score or (
            score == best_score
            and (best_work_id is None or candidate_id < best_work_id)
        ):
            best_score = score
            best_work_id = candidate_id

    if best_score >= threshold and best_work_id is not None:
        return best_work_id
    return None


def _create_local_work(
    file: LibraryFile,
    base_title: str,
    norm_artist: str,
    artist_repo: ArtistRepository,
    work_repo: WorkRepository,
    song_master_repo: SongMasterRepository,
) -> str:
    """Create a local artist + work + song master; return the new work_id."""
    raw_artist = file.audio.artist_name or ""
    artist_id = artist_repo.upsert_local_artist(raw_artist, norm_artist)
    work_id = work_repo.create_local(base_title, artist_id)
    song_master_repo.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file.id,
            selection_method=SelectionMethod.AUTO,
        ),
    )
    return work_id


def assign_work(
    file: LibraryFile,
    *,
    artist_repo: ArtistRepository,
    work_repo: WorkRepository,
    library_file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    song_master_repo: SongMasterRepository,
) -> GroupingResult | None:
    """Assign a work_id to the given LibraryFile.

    Returns GroupingResult with work_id and recording_id, or None if the
    file lacks artist/title metadata.
    """
    raw_artist = file.audio.artist_name or ""
    raw_title = file.audio.track_title or ""
    if not raw_artist.strip() or not raw_title.strip():
        return None

    shortcut = _try_hash_shortcut(file, library_file_repo)
    if shortcut is not None:
        return shortcut

    shortcut = _try_mbid_shortcut(file, recording_repo)
    if shortcut is not None:
        return shortcut

    base_title, version_type = extract_version_info(raw_title)
    norm_artist = normalize_artist(raw_artist)
    norm_base = normalize_title(base_title)

    work_id = _fuzzy_match_work(norm_base, norm_artist, work_repo)

    if work_id is None:
        work_id = _create_local_work(
            file, base_title, norm_artist, artist_repo, work_repo, song_master_repo
        )

    recording_id = recording_repo.get_or_create_local(
        work_id, version_type.value, raw_title,
    )
    logger.info(
        "grouping_recording_linked",
        extra={
            "work_id": work_id,
            "recording_id": recording_id,
            "version_type": version_type.value,
            "raw_title": raw_title,
        },
    )

    return GroupingResult(work_id=work_id, recording_id=recording_id)
