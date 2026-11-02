"""Pass 4 of library enrichment: files with no MusicBrainz tags under an artist with an MBID.

The three tag-driven passes never reach a file whose tags carry no MBID (a
2026-11-02 profile of the dev DB: 7,089 files, 4,230 of them under an artist
the local-artist linker gave a MusicBrainz MBID, AUD-R026). This pass searches
MusicBrainz recordings by (artist MBID, title), the search song matching
already makes, once per distinct pair, and hands a confident pick to the
per-recording path (:func:`library_enrichment_service.enrich_files_by_recording`)
so the file gets its recording, work, duration and live flag the normal way.

The confidence rule is :func:`pick_recording`. A file it cannot decide stays
skipped and is listed, never guessed: a wrong link would move the file onto
another song's work, and an automated decision that could be wrong must widen
the human review window, not decide silently (AUD-R100/R101).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from backend.domain.enums import EnrichmentStatus, VersionType
from backend.domain.library import LibraryFile, UntaggedCandidate
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    enrich_files_by_recording,
    recording_version_type,
)
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.mb_types import MbRecording
from backend.services.normalization import extract_version_info, normalize_title

logger = structlog.get_logger()

# How far a recording's length may sit from the file's duration and still be the same
# recording. MusicBrainz lengths of one recording differ by a second or two between releases;
# an edit, a single version or another take differs by tens of seconds.
DURATION_TOLERANCE_MS = 5_000


class PickOutcome(StrEnum):
    CONFIDENT = "confident"
    AMBIGUOUS = "ambiguous"
    EMPTY = "empty"


@dataclass(frozen=True)
class RecordingPick:
    """What the search said about one file.

    ``candidate_mbids`` are the hits that carry the file's title (version suffix aside),
    are credited to the artist and are compatible with the file's version; the pick is
    one of them, or None.
    """

    outcome: PickOutcome
    recording_mbid: str | None
    candidate_mbids: tuple[str, ...]


def search_title(track_title: str) -> str:
    """The title the search is made with: the tag's title without its version suffix
    ("[Album Version]", "(Radio Edit)"), normalized as song matching normalizes a
    broadcast title, so both share one cache entry per (artist, title)."""
    base, _ = extract_version_info(track_title)
    return normalize_title(base)


def _credited_to(hit: MbRecording, artist_mbid: str) -> bool:
    return any(
        (credit.get("artist") or {}).get("id") == artist_mbid
        for credit in hit.get("artist-credit", [])
    )


def _version_compatible(file_version: VersionType, hit_version: VersionType) -> bool:
    """Live is the one version MusicBrainz states outright, so it must agree both ways. A
    plain-titled recording may be the edit or album version a file is labelled as (MusicBrainz
    titles those with the song name and tells them apart by length); a recording titled as
    some other version ("(Strip mix)") is only that version."""
    if (file_version == VersionType.LIVE) != (hit_version == VersionType.LIVE):
        return False
    return hit_version in (VersionType.ORIGINAL, file_version)


def pick_recording(
    library_file: LibraryFile,
    artist_mbid: str,
    hits: list[MbRecording],
    tolerance_ms: int = DURATION_TOLERANCE_MS,
) -> RecordingPick:
    """The confidence rule: which search hit is this file, if the answer is unambiguous.

    A hit is a candidate when it is credited to the artist, its title equals the file's
    once both lose their version suffix and are normalized (exact, not fuzzy), and its
    version is compatible (:func:`_version_compatible`). No candidate: EMPTY. The pick is
    the one candidate whose length lies within ``tolerance_ms`` of the file's duration;
    a candidate with no length cannot be it. Several within the tolerance, or none (a
    version MusicBrainz does not list): AMBIGUOUS. A file with no duration is picked only
    when it has a single candidate.
    """
    title = library_file.audio.track_title or ""
    wanted = search_title(title)
    _, file_version = extract_version_info(title)
    candidates = [
        hit
        for hit in hits
        if hit.get("id")
        and _credited_to(hit, artist_mbid)
        and search_title(hit.get("title", "")) == wanted
        and _version_compatible(file_version, recording_version_type(hit))
    ]
    candidate_mbids = tuple(hit["id"] for hit in candidates)
    if not candidates:
        return RecordingPick(PickOutcome.EMPTY, None, ())

    duration = library_file.audio.duration_ms
    if duration is None:
        chosen = candidates if len(candidates) == 1 else []
    else:
        chosen = [
            hit
            for hit in candidates
            if hit.get("length") is not None and abs(hit["length"] - duration) <= tolerance_ms
        ]
    if len(chosen) == 1:
        return RecordingPick(PickOutcome.CONFIDENT, chosen[0]["id"], candidate_mbids)
    return RecordingPick(PickOutcome.AMBIGUOUS, None, candidate_mbids)


@dataclass(frozen=True)
class UntaggedPair:
    """The files one search answers for: one artist, one search title."""

    artist_mbid: str
    title: str
    files: tuple[LibraryFile, ...]


def untagged_pairs(candidates: list[UntaggedCandidate]) -> list[UntaggedPair]:
    """Group the candidates by (artist MBID, search title), in first-seen order, so the pass
    makes one search per pair. A file with no title to search is left out (untouched)."""
    groups: dict[tuple[str, str], list[LibraryFile]] = {}
    for candidate in candidates:
        title = search_title(candidate.file.audio.track_title or "")
        if not title:
            continue
        groups.setdefault((candidate.artist_mbid, title), []).append(candidate.file)
    return [
        UntaggedPair(artist_mbid=artist_mbid, title=title, files=tuple(files))
        for (artist_mbid, title), files in groups.items()
    ]


@dataclass(frozen=True)
class SkippedFile:
    """A file the pass left skipped, for review: why, and which hits were in the running."""

    file_path: str
    outcome: PickOutcome
    candidate_mbids: tuple[str, ...]


@dataclass(frozen=True)
class PairOutcome:
    enriched: int
    skipped: tuple[SkippedFile, ...]


def enrich_untagged_pair(
    pair: UntaggedPair,
    repos: EnrichmentRepos,
    mb_client: MusicBrainzClientProtocol,
) -> PairOutcome:
    """One search for the pair, then the per-recording path for every confident file.

    The client reads ``mb_cache`` before MusicBrainz, so a pair song matching already
    searched costs no call. Files picking the same recording share its lookup. A file the
    rule cannot decide is marked SKIPPED (a candidate again next run, which the cache then
    answers) and returned for review.
    """
    hits = mb_client.search_recording(artist_mbid=pair.artist_mbid, title=pair.title)
    by_recording: dict[str, list[LibraryFile]] = {}
    skipped: list[SkippedFile] = []
    for library_file in pair.files:
        pick = pick_recording(library_file, pair.artist_mbid, hits)
        if pick.recording_mbid is None:
            repos.files.update_recording_link(library_file.id, None, EnrichmentStatus.SKIPPED)
            skipped.append(SkippedFile(library_file.file_path, pick.outcome, pick.candidate_mbids))
            logger.debug(
                "untagged_file_skipped",
                file_path=library_file.file_path,
                outcome=pick.outcome.value,
                candidates=len(pick.candidate_mbids),
            )
        else:
            by_recording.setdefault(pick.recording_mbid, []).append(library_file)

    enriched = sum(
        enrich_files_by_recording(recording_mbid, files, repos, mb_client)
        for recording_mbid, files in by_recording.items()
    )
    logger.info(
        "enrich_untagged_pair_complete",
        artist_mbid=pair.artist_mbid,
        title=pair.title,
        files=len(pair.files),
        enriched=enriched,
        skipped=len(skipped),
    )
    return PairOutcome(enriched=enriched, skipped=tuple(skipped))
