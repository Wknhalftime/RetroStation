from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS, MusicBrainzId, Recording, Work
from backend.domain.enums import CatalogSource, EnrichmentStatus, VersionType
from backend.domain.library import LibraryFile
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.library_file_enrichment import LibraryFileEnrichmentRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.performances import PerformanceRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.grouping_service import exact_title_key
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.mb_types import MbArtistCredit, MbRecording, MbRelation, MbRelease
from backend.services.normalization import extract_version_info, normalize_artist
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work

logger = structlog.get_logger()

_LIVE_WORD_RE = re.compile(r"\blive\b", re.IGNORECASE)


@dataclass(frozen=True)
class EnrichmentRepos:
    """The repositories enrichment reads pending files from and writes links through.

    ``files`` and ``enrichment_queries`` are two ports of the library file
    repository (the composition root passes the same instance for both).
    """

    files: LibraryFileRepository
    enrichment_queries: LibraryFileEnrichmentRepository
    recordings: RecordingRepository
    works: WorkRepository
    song_masters: SongMasterRepository
    matches: MatchRepository
    artists: ArtistCatalogRepository
    # Phase 3a (S3): optional, so the locked tests' bundles still build; None skips it.
    performances: PerformanceRepository | None = None


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


def _upsert_artist_from_credits(
    credits: list[MbArtistCredit],
    artist_repo: ArtistCatalogRepository,
) -> str | None:
    """Upsert the artist named by the first usable artist-credit entry, and each further credit
    (phase 2a S8); return the first's id.

    Shared by `enrich_by_release`, `enrich_by_recording` and
    `enrich_by_recording_batch` — the block always changed identically across
    every commit that touched it (see the PR description for the git-log
    evidence), so it is one function, not three copies.
    """
    artist_info = _extract_artist_from_credits(credits)
    if artist_info is None:
        return None
    artist_mbid, artist_name, artist_sort_name = artist_info
    artist_id = artist_repo.upsert_musicbrainz_artist(
        mbid=artist_mbid,
        name=artist_name,
        sort_name=artist_sort_name,
        normalized_name=normalize_artist(artist_name),
    )
    _upsert_further_credits(credits, artist_mbid, artist_repo)
    return artist_id


def _upsert_further_credits(
    credits: list[MbArtistCredit], first_mbid: str, artist_repo: ArtistCatalogRepository
) -> None:
    """Each credited artist after the first (phase 2a S8), but a special-purpose one. The work
    keeps the first until 3b."""
    for credit in credits:
        artist = credit.get("artist") or {}
        mbid, name = artist.get("id"), artist.get("name")
        if not mbid or not name or mbid == first_mbid or mbid in SPECIAL_PURPOSE_MBIDS:
            continue
        artist_repo.upsert_further_credit(
            mbid=mbid,
            name=name,
            sort_name=artist.get("sort-name") or name,
            normalized_name=normalize_artist(name),
        )


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


def _musicbrainz_marks_live(rec_data: MbRecording) -> bool:
    """Whether MusicBrainz records this recording as a live performance.

    MusicBrainz titles a live recording with the plain song name and says it
    is live elsewhere: a "live" attribute on the performance relation (only
    lookups carry relations) or "live" in the disambiguation ("live,
    1988-08-19: Nassau Coliseum"; searches carry this too). The title-suffix
    rules are not used on the disambiguation: they read "radio edit" as an
    edit and a bare date ("1951-10-01 recording") as a concert.
    """
    for rel in rec_data.get("relations", []):
        if rel.get("type") == "performance" and "live" in rel.get("attributes", []):
            return True
    return _LIVE_WORD_RE.search(_text(rec_data, "disambiguation")) is not None


def _text(rec_data: MbRecording, key: str) -> str:
    """A payload's text field, or "" when it is absent or not text (a JSON null, say)."""
    value: object = rec_data.get(key, "")
    return value if isinstance(value, str) else ""


def recording_version_type(rec_data: MbRecording) -> VersionType:
    """The version a MusicBrainz recording is: live if MusicBrainz says so, else from its title."""
    if _musicbrainz_marks_live(rec_data):
        return VersionType.LIVE
    _, version_type = extract_version_info(_text(rec_data, "title"))
    return version_type


@dataclass(frozen=True)
class PerformedWork:
    """The work a recording performs, and whether a local work just took its MBID."""

    work_id: str
    claimed: bool


def _same_song_local_work(work: Work, artist_id: str, work_title: str) -> bool:
    """Whether *work* is the local work of this song: local, no MBID, the artist the step
    gives the work, and a title equal by grouping's exact path (AUD-R103, R1)."""
    return (
        work.origin == CatalogSource.LOCAL
        and work.mbid is None
        and work.artist_id == artist_id
        and exact_title_key(work.title) == exact_title_key(work_title)
    )


def _performed_work(
    work_mbid: str,
    work_title: str,
    artist_id: str,
    files: Sequence[LibraryFile],
    repos: EnrichmentRepos,
) -> PerformedWork:
    """The work holding *work_mbid*; else the local work of the first file (by id) that is
    this song's, which takes the MBID so every copy on it stays with it (AUD-R103, R1); else
    a new MusicBrainz work. Works are never merged: where one holds the MBID, the caller's
    per-file move stays and a pair waits for the merge list."""
    existing = repos.works.get_id_by_mbid(work_mbid)
    if existing is not None:
        return PerformedWork(existing, claimed=False)
    for library_file in sorted(files, key=lambda f: f.id):
        work = repos.works.get_by_id(library_file.work_id) if library_file.work_id else None
        if work is None or not _same_song_local_work(work, artist_id, work_title):
            continue
        if repos.works.claim_mbid(work.id, work_mbid, work_title):
            logger.info(
                "local_work_took_mbid",
                work_id=work.id,
                work_mbid=work_mbid,
                file_id=str(library_file.id),
            )
            return PerformedWork(work.id, claimed=True)
    work_id = repos.works.upsert_from_mb(mbid=work_mbid, title=work_title, artist_id=artist_id)
    return PerformedWork(work_id, claimed=False)


def _put_recording_on_work(
    recording_id: str, performed: PerformedWork, repos: EnrichmentRepos
) -> bool:
    """Put the recording on its work where the version slot is free (C1); whether it is.

    A local work that just took the MBID drops grouping's recordings no file holds first, so
    the MusicBrainz recording can take the slot a lone copy's grouping recording held.
    """
    if performed.claimed:
        repos.works.drop_fileless_grouping_recordings(performed.work_id)
    return repos.recordings.link_work(recording_id, performed.work_id)


def _upsert_recording_with_work(
    rec_mbid: str,
    rec_data: MbRecording,
    artist_id: str | None,
    repos: EnrichmentRepos,
    *,
    files: Sequence[LibraryFile] = (),
) -> PerformedWork | None:
    """Upsert the recording, without its work (C1), and resolve the work it performs.

    *files* are the ones the caller links; a local work of theirs may take the MBID
    (:func:`_performed_work`). The caller links them to the recording, then puts the
    recording on the work (:func:`_put_recording_on_work`), so a version slot another
    recording holds leaves it off the work instead of raising on
    ``uq_recordings_work_version``.
    """
    relations: list[MbRelation] = rec_data.get("relations", [])
    work_info = _extract_work_from_relations(relations)
    performed: PerformedWork | None = None
    if work_info and artist_id is not None:
        work_mbid, work_title = work_info
        performed = _performed_work(work_mbid, work_title, artist_id, files, repos)

    repos.recordings.upsert(
        Recording(
            id=rec_mbid,
            title=rec_data.get("title", ""),
            work_id=None,
            duration_ms=rec_data.get("length"),
            version_type=recording_version_type(rec_data),
        )
    )
    return performed


def _mark_files_failed(pending_files: list[LibraryFile], files: LibraryFileRepository) -> None:
    """Mark files FAILED so the next enrichment run's pending query skips them."""
    for library_file in pending_files:
        files.update_recording_link(library_file.id, None, EnrichmentStatus.FAILED)


def _link_file_to_recording(
    library_file: LibraryFile,
    recording_mbid: str,
    files: LibraryFileRepository,
) -> None:
    """Mark a library file as ENRICHED and link it to its recording."""
    files.update_recording_link(library_file.id, recording_mbid, EnrichmentStatus.ENRICHED)
    logger.debug(
        "library_file_enriched",
        file_id=str(library_file.id),
        recording_mbid=recording_mbid,
    )


def _move_file_to_work(
    library_file: LibraryFile,
    work_id: str,
    repos: EnrichmentRepos,
) -> None:
    """Attach a file to the work MusicBrainz says its recording performs.

    Grouping gave the file a local work at scan time; see
    :func:`work_move_service.move_file_to_work` for what the move settles.
    """
    move_file_to_work(
        library_file,
        work_id,
        WorkMoveRepos(
            files=repos.files,
            works=repos.works,
            song_masters=repos.song_masters,
            matches=repos.matches,
            performances=repos.performances,
        ),
    )


def needs_recording_lookup(recording: Recording, musicbrainz_work_ids: set[str]) -> bool:
    """Whether a recording lookup can still tell us something: no length, or no MB work.

    The batched search that creates most recordings stores the length but
    carries no relations, so a recording on no MusicBrainz work (none, or a
    local one from grouping) may still perform a work the lookup names.
    ``musicbrainz_work_ids`` are the MusicBrainz works among the pending
    recordings' works (``WorkRepository.list_musicbrainz_ids``).
    """
    return recording.duration_ms is None or recording.work_id not in musicbrainz_work_ids


@dataclass(frozen=True)
class RecordingLookupApplied:
    """What one recording lookup's payload changed about the stored recording."""

    work_id: str | None
    """The work the payload says the recording performs, once upserted; else None."""
    recording_linked: bool
    """Whether ``recordings.work_id`` was set to it (False when the version slot is taken)."""
    files_moved: int
    marked_live: bool
    duration_set: bool


def apply_recording_lookup(
    recording: Recording,
    rec_data: MbRecording,
    repos: EnrichmentRepos,
) -> RecordingLookupApplied:
    """Write what a per-recording lookup carries beyond the title: length, live, work.

    The length fills a missing duration only. The live flag is the one
    version MusicBrainz states outright (:func:`recording_version_type`);
    another version the title now reads as is left as stored, since the
    title has not changed. The performed work is upserted with the track
    artist, as the per-release path does, the recording is put on it where
    its version slot is free, and every file of the recording moves onto it
    (see :func:`work_move_service.move_file_to_work` for what a move settles).
    """
    duration_set = False
    length = rec_data.get("length")
    if recording.duration_ms is None and length is not None:
        repos.recordings.update_duration(recording.id, int(length))
        duration_set = True

    marked_live = False
    if (
        recording.version_type != VersionType.LIVE
        and recording_version_type(rec_data) == VersionType.LIVE
    ):
        marked_live = repos.recordings.update_version_type(recording.id, VersionType.LIVE)

    files = sorted(repos.files.get_by_recording(recording.id), key=lambda f: f.id)
    performed = _upsert_performed_work(rec_data, files, repos)
    recording_linked = False
    files_moved = 0
    if performed is not None:
        if recording.work_id != performed.work_id:
            recording_linked = _put_recording_on_work(recording.id, performed, repos)
        for library_file in files:
            if library_file.work_id != performed.work_id:
                _move_file_to_work(library_file, performed.work_id, repos)
                files_moved += 1

    return RecordingLookupApplied(
        work_id=performed.work_id if performed is not None else None,
        recording_linked=recording_linked,
        files_moved=files_moved,
        marked_live=marked_live,
        duration_set=duration_set,
    )


def performed_work_of(rec_data: MbRecording) -> tuple[str, str] | None:
    """(work mbid, work title) of the work a recording payload says it performs, if any."""
    return _extract_work_from_relations(rec_data.get("relations", []))


def _upsert_performed_work(
    rec_data: MbRecording, files: Sequence[LibraryFile], repos: EnrichmentRepos
) -> PerformedWork | None:
    """The work the payload's performance relation names, under its track artist
    (:func:`_performed_work`)."""
    work_info = performed_work_of(rec_data)
    if work_info is None:
        return None
    artist_id = _upsert_artist_from_credits(rec_data.get("artist-credit", []), repos.artists)
    if artist_id is None:
        return None
    work_mbid, work_title = work_info
    return _performed_work(work_mbid, work_title, artist_id, files, repos)


def _resolve_track_recording(
    rec_mbid: str,
    recording_map: dict[str, MbRecording],
    mb_client: MusicBrainzClientProtocol,
) -> str | None:
    """The MBID under which *rec_mbid* appears on the release, or None.

    A tag can name a recording MusicBrainz has since merged into another;
    the release then lists only the survivor. One direct lookup (the client
    follows the redirect) tells which recording that is. A recording that
    is genuinely not on the release stays unresolved.
    """
    if rec_mbid in recording_map:
        return rec_mbid
    if MusicBrainzId.parse(rec_mbid) is None:
        return None
    survivor = mb_client.lookup_recording(rec_mbid)
    if survivor is None:
        return None
    survivor_id = survivor.get("id")
    if survivor_id and survivor_id != rec_mbid and survivor_id in recording_map:
        logger.info(
            "recording_mbid_merged",
            recording_mbid=rec_mbid,
            survivor_mbid=survivor_id,
        )
        return survivor_id
    return None


def _build_recording_map(release_data: MbRelease) -> dict[str, MbRecording]:
    """Map recording_mbid -> recording dict from every track on the release.

    A track with no recording, or a recording with no id (an enhanced-CD
    data track, say), contributes nothing to the map.
    """
    recording_map: dict[str, MbRecording] = {}
    for medium in release_data.get("media", []):
        for track in medium.get("tracks", []):
            rec = track.get("recording")
            if rec and rec.get("id"):
                recording_map[rec["id"]] = rec
    return recording_map


@dataclass(frozen=True)
class _ReleaseLookup:
    """What one release lookup resolved: its recordings and (if found) artist."""

    recording_map: dict[str, MbRecording]
    artist_id: str | None
    release_mbid: str


def _enrich_one_file(
    library_file: LibraryFile,
    release: _ReleaseLookup,
    repos: EnrichmentRepos,
    mb_client: MusicBrainzClientProtocol,
) -> bool:
    """Enrich one pending file against a release's recording map.

    Returns whether the file was enriched. A file with no recording_mbid
    tag, or one that resolves to nothing on this release (see
    `_resolve_track_recording`), fails instead.
    """
    rec_mbid = library_file.audio.recording_mbid
    if not rec_mbid:
        logger.debug("library_file_no_recording_mbid", file_id=str(library_file.id))
        repos.files.update_recording_link(library_file.id, None, EnrichmentStatus.FAILED)
        return False

    resolved_mbid = _resolve_track_recording(rec_mbid, release.recording_map, mb_client)
    if resolved_mbid is None:
        logger.warning(
            "recording_not_found_in_release",
            recording_mbid=rec_mbid,
            release_mbid=release.release_mbid,
        )
        repos.files.update_recording_link(library_file.id, None, EnrichmentStatus.FAILED)
        return False

    performed = _upsert_recording_with_work(
        resolved_mbid,
        release.recording_map[resolved_mbid],
        release.artist_id,
        repos,
        files=[library_file],
    )
    _link_file_to_recording(library_file, resolved_mbid, repos.files)
    if performed is not None:
        _put_recording_on_work(resolved_mbid, performed, repos)
        _move_file_to_work(library_file, performed.work_id, repos)
    return True


def enrich_by_release(
    release_mbid: str,
    repos: EnrichmentRepos,
    mb_client: MusicBrainzClientProtocol,
) -> int:
    """Enrich all pending library files that belong to the given release.

    Looks up the release once (recordings with their work relations), upserts
    the artist, each recording and its performed work, then links each
    pending file to its Recording row. Returns the count of files enriched.
    A malformed release_mbid (e.g. a corrupt tag) fails its files without a
    lookup — MusicBrainz would answer 400, a permanent failure.
    """
    pending_files = repos.enrichment_queries.get_pending_enrichment_by_release(release_mbid)
    if not pending_files:
        return 0

    release_id = MusicBrainzId.parse(release_mbid)
    if release_id is None:
        logger.warning("malformed_release_mbid", release_mbid=release_mbid)
        _mark_files_failed(pending_files, repos.files)
        return 0

    release_data = mb_client.lookup_release(release_id.value)
    if release_data is None:
        logger.warning("mb_release_lookup_failed", release_mbid=release_mbid)
        _mark_files_failed(pending_files, repos.files)
        return 0

    artist_id = _upsert_artist_from_credits(release_data.get("artist-credit", []), repos.artists)
    release = _ReleaseLookup(
        recording_map=_build_recording_map(release_data),
        artist_id=artist_id,
        release_mbid=release_mbid,
    )

    enriched_count = 0
    for library_file in pending_files:
        if _enrich_one_file(library_file, release, repos, mb_client):
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
    repos: EnrichmentRepos,
    mb_client: MusicBrainzClientProtocol,
) -> int:
    """Enrich pending library files that have a recording_mbid but no release_mbid.

    Looks up the recording directly. Returns count of files enriched.
    A malformed recording_mbid fails its files without a lookup, as in
    enrich_by_release.
    """
    pending_files = repos.enrichment_queries.get_pending_enrichment_by_recording(recording_mbid)
    if not pending_files:
        return 0
    return enrich_files_by_recording(recording_mbid, pending_files, repos, mb_client)


def enrich_files_by_recording(
    recording_mbid: str,
    pending_files: list[LibraryFile],
    repos: EnrichmentRepos,
    mb_client: MusicBrainzClientProtocol,
) -> int:
    """Link *pending_files* to one recording from its lookup; the step behind
    :func:`enrich_by_recording`, also fed by the untagged-file pass
    (:mod:`untagged_recording_service`), whose files carry no recording tag.

    One lookup gives the recording its title, length, live flag and performed
    work; every file is linked to it and moved onto that work. Returns the
    count of files enriched; a lookup that fails marks them all FAILED.
    """
    recording_id = MusicBrainzId.parse(recording_mbid)
    if recording_id is None:
        logger.warning("malformed_recording_mbid", recording_mbid=recording_mbid)
        _mark_files_failed(pending_files, repos.files)
        return 0

    rec_data = mb_client.lookup_recording(recording_id.value)
    if rec_data is None:
        logger.warning("mb_recording_lookup_failed", recording_mbid=recording_mbid)
        _mark_files_failed(pending_files, repos.files)
        return 0

    artist_id = _upsert_artist_from_credits(rec_data.get("artist-credit", []), repos.artists)
    performed = _upsert_recording_with_work(
        recording_mbid, rec_data, artist_id, repos, files=pending_files
    )

    for library_file in pending_files:
        _link_file_to_recording(library_file, recording_mbid, repos.files)
    if performed is not None:
        _put_recording_on_work(recording_mbid, performed, repos)
        for library_file in pending_files:
            _move_file_to_work(library_file, performed.work_id, repos)
    enriched_count = len(pending_files)

    logger.info(
        "enrich_by_recording_complete",
        recording_mbid=recording_mbid,
        enriched=enriched_count,
    )
    return enriched_count


@dataclass(frozen=True)
class BatchEnrichment:
    """What one batched pass did: files linked, and files left pending."""

    enriched: int
    unresolved: tuple[LibraryFile, ...]


def enrich_by_recording_batch(
    pending_files: list[LibraryFile],
    repos: EnrichmentRepos,
    mb_client: MusicBrainzClientProtocol,
) -> BatchEnrichment:
    """Link pending files from one recording search instead of a lookup per release.

    Every distinct recording MBID goes into ``search_recordings_by_mbids``
    (100 per request). A file is linked when its recording came back and
    lists the file's release; the recording's own artist credit (the track
    artist) is upserted. Anything else, a recording MusicBrainz no longer
    knows under that MBID, a recording no longer on that release, a file
    with no recording MBID, is returned as unresolved and left pending,
    untouched, for the per-release path to handle as it does today. Search
    results carry no work relations, so no work is linked here; only the
    per-release and per-recording lookups link works.
    """
    searchable = [f for f in pending_files if f.audio.recording_mbid]
    found = mb_client.search_recordings_by_mbids(
        list(dict.fromkeys(f.audio.recording_mbid for f in searchable if f.audio.recording_mbid))
    )

    unresolved: list[LibraryFile] = [f for f in pending_files if not f.audio.recording_mbid]
    enriched = 0
    for library_file in searchable:
        rec_mbid = library_file.audio.recording_mbid
        assert rec_mbid is not None
        rec_data = found.get(rec_mbid)
        release_mbid = library_file.audio.release_mbid
        on_release = rec_data is not None and (
            release_mbid is None
            or any(r.get("id") == release_mbid for r in rec_data.get("releases", []))
        )
        if rec_data is None or not on_release:
            unresolved.append(library_file)
            continue
        _upsert_artist_from_credits(rec_data.get("artist-credit", []), repos.artists)
        repos.recordings.upsert(
            Recording(
                id=rec_mbid,
                title=rec_data.get("title", ""),
                work_id=None,
                duration_ms=rec_data.get("length"),
                version_type=recording_version_type(rec_data),
            )
        )
        _link_file_to_recording(library_file, rec_mbid, repos.files)
        enriched += 1

    logger.info(
        "enrich_by_recording_batch_complete",
        files=len(pending_files),
        enriched=enriched,
        unresolved=len(unresolved),
    )
    return BatchEnrichment(enriched=enriched, unresolved=tuple(unresolved))
