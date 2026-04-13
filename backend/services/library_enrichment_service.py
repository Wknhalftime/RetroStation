from __future__ import annotations

from typing import Any, Protocol

import structlog

from backend.domain.catalog import Recording
from backend.domain.enums import EnrichmentStatus
from backend.repositories.artists import ArtistRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.works import WorkRepository
from backend.services.normalization import extract_version_info, normalize_artist

logger = structlog.get_logger()


class MbClientProtocol(Protocol):
    def lookup_release(self, mbid: str) -> dict[str, Any] | None: ...
    def lookup_recording(self, mbid: str) -> dict[str, Any] | None: ...


def _extract_artist_from_credits(
    credits: list[dict[str, Any]],
) -> tuple[str, str, str] | None:
    """Return (mbid, name, sort_name) from the first artist-credit entry, or None."""
    for credit in credits:
        artist = credit.get("artist")
        if not artist:
            continue
        mbid = artist.get("id")
        name = artist.get("name")
        sort_name = artist.get("sort-name") or name
        if mbid and name:
            return (mbid, name, sort_name)
    return None


def _extract_work_from_relations(
    relations: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Return (work_mbid, work_title) from the first 'performance' relation, or None."""
    for rel in relations:
        if rel.get("type") == "performance":
            work = rel.get("work")
            if work:
                work_mbid = work.get("id")
                work_title = work.get("title")
                if work_mbid and work_title:
                    return (work_mbid, work_title)
    return None


def enrich_by_release(
    release_mbid: str,
    library_file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    artist_repo: ArtistRepository,
    mb_client: MbClientProtocol,
) -> int:
    """Enrich all pending library files that belong to the given release.

    Looks up the release once, extracts artist/recordings/works, then links
    each pending file to its Recording row. Returns the count of files enriched.
    """
    pending_files = library_file_repo.get_pending_enrichment_by_release(release_mbid)
    if not pending_files:
        return 0

    release_data = mb_client.lookup_release(release_mbid)
    if release_data is None:
        logger.warning("mb_release_lookup_failed", release_mbid=release_mbid)
        for lf in pending_files:
            library_file_repo.update_recording_link(
                lf.id, None, EnrichmentStatus.FAILED
            )
        return 0

    # Extract artist from release-level credits
    artist_credits: list[dict[str, Any]] = release_data.get("artist-credit", [])
    artist_info = _extract_artist_from_credits(artist_credits)
    artist_id: str | None = None
    if artist_info:
        artist_mbid, artist_name, artist_sort_name = artist_info
        artist_id = artist_repo.upsert_musicbrainz_artist(
            mbid=artist_mbid,
            name=artist_name,
            sort_name=artist_sort_name,
            normalized_name=normalize_artist(artist_name),
        )

    # Build recording map: recording_mbid -> recording dict from media tracks
    recording_map: dict[str, dict[str, Any]] = {}
    for medium in release_data.get("media", []):
        for track in medium.get("tracks", []):
            rec = track.get("recording")
            if rec and rec.get("id"):
                recording_map[rec["id"]] = rec

    enriched_count = 0
    for lf in pending_files:
        rec_mbid = lf.audio.recording_mbid
        if not rec_mbid:
            logger.debug("library_file_no_recording_mbid", file_id=str(lf.id))
            library_file_repo.update_recording_link(
                lf.id, None, EnrichmentStatus.FAILED
            )
            continue

        rec_data = recording_map.get(rec_mbid)
        if rec_data is None:
            logger.warning(
                "recording_not_found_in_release",
                recording_mbid=rec_mbid,
                release_mbid=release_mbid,
            )
            library_file_repo.update_recording_link(
                lf.id, None, EnrichmentStatus.FAILED
            )
            continue

        # Extract work from recording relations
        relations: list[dict[str, Any]] = rec_data.get("relations", [])
        work_info = _extract_work_from_relations(relations)
        work_id: str | None = None
        if work_info and artist_id is not None:
            work_mbid, work_title = work_info
            work_id = work_repo.upsert_from_mb(
                mbid=work_mbid,
                title=work_title,
                artist_id=artist_id,
            )

        # Upsert recording — derive version_type from the recording title
        rec_title = rec_data.get("title", "")
        _, version_type = extract_version_info(rec_title)
        recording_repo.upsert(Recording(
            id=rec_mbid,
            title=rec_title,
            work_id=work_id,
            duration_ms=rec_data.get("length"),
            version_type=version_type,
        ))

        library_file_repo.update_recording_link(
            lf.id, rec_mbid, EnrichmentStatus.ENRICHED
        )

        # Sync work_id to library_file
        if work_id is not None:
            library_file_repo.update_work_id(lf.id, work_id)

        enriched_count += 1
        logger.debug(
            "library_file_enriched",
            file_id=str(lf.id),
            recording_mbid=rec_mbid,
        )

    logger.info(
        "enrich_by_release_complete",
        release_mbid=release_mbid,
        enriched=enriched_count,
        total=len(pending_files),
    )
    return enriched_count


def enrich_by_recording(
    recording_mbid: str,
    library_file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    artist_repo: ArtistRepository,
    mb_client: MbClientProtocol,
) -> int:
    """Enrich pending library files that have a recording_mbid but no release_mbid.

    Looks up the recording directly. Returns count of files enriched.
    """
    pending_files = library_file_repo.get_pending_enrichment_by_recording(recording_mbid)
    if not pending_files:
        return 0

    rec_data = mb_client.lookup_recording(recording_mbid)
    if rec_data is None:
        logger.warning("mb_recording_lookup_failed", recording_mbid=recording_mbid)
        for lf in pending_files:
            library_file_repo.update_recording_link(
                lf.id, None, EnrichmentStatus.FAILED
            )
        return 0

    # Extract artist from recording-level credits
    artist_credits: list[dict[str, Any]] = rec_data.get("artist-credit", [])
    artist_info = _extract_artist_from_credits(artist_credits)
    artist_id: str | None = None
    if artist_info:
        artist_mbid, artist_name, artist_sort_name = artist_info
        artist_id = artist_repo.upsert_musicbrainz_artist(
            mbid=artist_mbid,
            name=artist_name,
            sort_name=artist_sort_name,
            normalized_name=normalize_artist(artist_name),
        )

    # Extract work from relations
    relations: list[dict[str, Any]] = rec_data.get("relations", [])
    work_info = _extract_work_from_relations(relations)
    work_id: str | None = None
    if work_info and artist_id is not None:
        work_mbid, work_title = work_info
        work_id = work_repo.upsert_from_mb(
            mbid=work_mbid,
            title=work_title,
            artist_id=artist_id,
        )

    # Upsert recording — derive version_type from the recording title
    rec_title = rec_data.get("title", "")
    _, version_type = extract_version_info(rec_title)
    recording_repo.upsert(Recording(
        id=recording_mbid,
        title=rec_title,
        work_id=work_id,
        duration_ms=rec_data.get("length"),
        version_type=version_type,
    ))

    enriched_count = 0
    for lf in pending_files:
        library_file_repo.update_recording_link(
            lf.id, recording_mbid, EnrichmentStatus.ENRICHED
        )
        # Sync work_id to library_file
        if work_id is not None:
            library_file_repo.update_work_id(lf.id, work_id)
        enriched_count += 1
        logger.debug(
            "library_file_enriched",
            file_id=str(lf.id),
            recording_mbid=recording_mbid,
        )

    logger.info(
        "enrich_by_recording_complete",
        recording_mbid=recording_mbid,
        enriched=enriched_count,
    )
    return enriched_count
