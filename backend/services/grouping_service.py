"""Local-first song grouping -- 4-step matching algorithm with version dedup."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from rapidfuzz import fuzz

from backend.domain.enums import SelectionMethod, VersionType
from backend.domain.models import LibraryFile, SongMaster
from backend.repositories.artists import ArtistRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.normalization import (
    classify_version_descriptor,
    detect_embedded_remix,
    extract_dash_version,
    extract_version_tags,
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


def _extract_version_info(raw_title: str) -> tuple[str, VersionType]:
    """Strip version tags from a raw title and classify the version type.

    Tries parenthetical tags first, then dash-separated suffixes, then
    embedded remix patterns.  Returns (base_title, version_type).
    """
    base, tags = extract_version_tags(raw_title)
    if tags:
        vtype = classify_version_descriptor(tags[0])
        if vtype != VersionType.UNKNOWN:
            return base, vtype

    base_dash, dash_tag = extract_dash_version(raw_title)
    if dash_tag is not None:
        vtype = classify_version_descriptor(dash_tag)
        if vtype != VersionType.UNKNOWN:
            return base_dash, vtype

    base_embed, embed_tag = detect_embedded_remix(raw_title)
    if embed_tag is not None:
        vtype = classify_version_descriptor(embed_tag)
        if vtype != VersionType.UNKNOWN:
            return base_embed, vtype

    return raw_title, VersionType.ORIGINAL


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

    # Step 1: Hash shortcut
    existing_by_hash = library_file_repo.get_by_hash(file.file_hash)
    for existing in existing_by_hash:
        if existing.work_id is not None and existing.id != file.id:
            return GroupingResult(
                work_id=existing.work_id,
                recording_id=existing.recording_id,
            )

    # Step 2: MBID shortcut
    if file.audio.recording_mbid:
        recording = recording_repo.get_by_id(file.audio.recording_mbid)
        if recording and recording.work_id:
            return GroupingResult(
                work_id=recording.work_id,
                recording_id=recording.id,
            )

    # Version extraction — strip tags before fuzzy matching
    base_title, version_type = _extract_version_info(raw_title)

    norm_artist = normalize_artist(raw_artist)
    norm_base = normalize_title(base_title)

    # Step 3: Artist-first fuzzy match against works.title
    work_id: str | None = None
    if norm_base:
        work_candidates = work_repo.get_candidates_by_normalized_artist(
            norm_artist, limit=100,
        )
        if work_candidates:
            threshold = _dynamic_threshold(len(norm_base))
            strict_input = strict_normalize(norm_base)

            best_work_id: str | None = None
            best_score = -1.0

            for candidate_id, candidate_title in work_candidates:
                norm_candidate = normalize_title(candidate_title)
                if strict_input == strict_normalize(norm_candidate):
                    score = 100.0
                else:
                    full = fuzz.ratio(norm_base, norm_candidate)
                    partial = fuzz.partial_ratio(
                        norm_base, norm_candidate,
                    )
                    score = 0.7 * full + 0.3 * partial

                if score > best_score or (
                    score == best_score
                    and (
                        best_work_id is None
                        or candidate_id < best_work_id
                    )
                ):
                    best_score = score
                    best_work_id = candidate_id

            if best_score >= threshold and best_work_id is not None:
                work_id = best_work_id

    # Step 4: Create local work if no match
    if work_id is None:
        artist_id = artist_repo.upsert_local(raw_artist, norm_artist)
        work_id = work_repo.create_local(base_title, artist_id)

        song_master_repo.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work_id,
                preferred_file_id=file.id,
                selection_method=SelectionMethod.AUTO,
            ),
        )

    # Create or reuse recording for this version
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
