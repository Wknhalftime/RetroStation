from __future__ import annotations

import structlog

from backend.domain.catalog import Recording
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import LibraryFile
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.library_file_enrichment import LibraryFileEnrichmentRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.works import WorkRepository
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.mb_types import MbArtistCredit, MbRecording, MbRelation
from backend.services.normalization import extract_version_info, normalize_artist

logger = structlog.get_logger()


def _extract_artist_from_credits(
    credits: list[MbArtistCredit],
) -> tuple[str, str, str] | None:
    """Return (mbid, name, sort_name) from the first artist-credit entry, or None."""
    for credit in credits:
        artist = credit.get("artist")
        if not artist:
            continue
        mbid = artist.get("id")
        name = artist.get("name")
        if mbid and name:
            sort_name: str = artist.get("sort-name") or name
            return (mbid, name, sort_name)
    return None


def _extract_work_from_relations(
    relations: list[MbRelation],
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


def _upsert_recording_with_work(
    rec_mbid: str,
    rec_data: MbRecording,
    artist_id: str | None,
    work_repo: WorkRepository,
    recording_repo: RecordingRepository,
) -> str | None:
    """Extract work from rec_data relations, upsert work + recording; return work_id or None."""
    relations: list[MbRelation] = rec_data.get("relations", [])
    work_info = _extract_work_from_relations(relations)
    work_id: str | None = None
    if work_info and artist_id is not None:
        work_mbid, work_title = work_info
        work_id = work_repo.upsert_from_mb(
            mbid=work_mbid,
            title=work_title,
            artist_id=artist_id,
        )

    rec_title = rec_data.get("title", "")
    _, version_type = extract_version_info(rec_title)
    recording_repo.upsert(Recording(
        id=rec_mbid,
        title=rec_title,
        work_id=work_id,
        duration_ms=rec_data.get("length"),
        version_type=version_type,
    ))
    return work_id


def _link_file_to_recording(
    library_file: LibraryFile,
    recording_mbid: str,
    work_id: str | None,
    files: LibraryFileRepository,
) -> None:
    """Mark a library file as ENRICHED and link it to its recording and optional work."""
    files.update_recording_link(
        library_file.id, recording_mbid, EnrichmentStatus.ENRICHED
    )
    if work_id is not None:
        files.update_work_id(library_file.id, work_id)
    logger.debug(
        "library_file_enriched",
        file_id=str(library_file.id),
        recording_mbid=recording_mbid,
    )


def enrich_by_release(
    release_mbid: str,
    files: LibraryFileRepository,
    enrichment_queries: LibraryFileEnrichmentRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    artist_repo: ArtistCatalogRepository,
    mb_client: MusicBrainzClientProtocol,
) -> int:
    """Enrich all pending library files that belong to the given release.

    Looks up the release once, extracts artist/recordings/works, then links
    each pending file to its Recording row. Returns the count of files enriched.
    """
    pending_files = enrichment_queries.get_pending_enrichment_by_release(release_mbid)
    if not pending_files:
        return 0

    release_data = mb_client.lookup_release(release_mbid)
    if release_data is None:
        logger.warning("mb_release_lookup_failed", release_mbid=release_mbid)
        for library_file in pending_files:
            files.update_recording_link(
                library_file.id, None, EnrichmentStatus.FAILED
            )
        return 0

    artist_credits: list[MbArtistCredit] = release_data.get("artist-credit", [])
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
    recording_map: dict[str, MbRecording] = {}
    for medium in release_data.get("media", []):
        for track in medium.get("tracks", []):
            rec = track.get("recording")
            if rec and rec.get("id"):
                recording_map[rec["id"]] = rec

    enriched_count = 0
    for library_file in pending_files:
        rec_mbid = library_file.audio.recording_mbid
        if not rec_mbid:
            logger.debug("library_file_no_recording_mbid", file_id=str(library_file.id))
            files.update_recording_link(
                library_file.id, None, EnrichmentStatus.FAILED
            )
            continue

        rec_data = recording_map.get(rec_mbid)
        if rec_data is None:
            logger.warning(
                "recording_not_found_in_release",
                recording_mbid=rec_mbid,
                release_mbid=release_mbid,
            )
            files.update_recording_link(
                library_file.id, None, EnrichmentStatus.FAILED
            )
            continue

        work_id = _upsert_recording_with_work(
            rec_mbid, rec_data, artist_id, work_repo, recording_repo
        )
        _link_file_to_recording(library_file, rec_mbid, work_id, files)
        enriched_count += 1

    logger.info(
        "enrich_by_release_complete",
        release_mbid=release_mbid,
        enriched=enriched_count,
        total=len(pending_files),
    )
    return enriched_count


def enrich_by_recording(
    recording_mbid: str,
    files: LibraryFileRepository,
    enrichment_queries: LibraryFileEnrichmentRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    artist_repo: ArtistCatalogRepository,
    mb_client: MusicBrainzClientProtocol,
) -> int:
    """Enrich pending library files that have a recording_mbid but no release_mbid.

    Looks up the recording directly. Returns count of files enriched.
    """
    pending_files = enrichment_queries.get_pending_enrichment_by_recording(recording_mbid)
    if not pending_files:
        return 0

    rec_data = mb_client.lookup_recording(recording_mbid)
    if rec_data is None:
        logger.warning("mb_recording_lookup_failed", recording_mbid=recording_mbid)
        for library_file in pending_files:
            files.update_recording_link(
                library_file.id, None, EnrichmentStatus.FAILED
            )
        return 0

    artist_credits: list[MbArtistCredit] = rec_data.get("artist-credit", [])
    artist_info = _extract_artist_from_credits(artist_credits)
    artist_id = None
    if artist_info:
        artist_mbid, artist_name, artist_sort_name = artist_info
        artist_id = artist_repo.upsert_musicbrainz_artist(
            mbid=artist_mbid,
            name=artist_name,
            sort_name=artist_sort_name,
            normalized_name=normalize_artist(artist_name),
        )

    work_id = _upsert_recording_with_work(
        recording_mbid, rec_data, artist_id, work_repo, recording_repo
    )

    enriched_count = 0
    for library_file in pending_files:
        _link_file_to_recording(library_file, recording_mbid, work_id, files)
        enriched_count += 1

    logger.info(
        "enrich_by_recording_complete",
        recording_mbid=recording_mbid,
        enriched=enriched_count,
    )
    return enriched_count
