"""Fake-backed builders for PR C's service tests, frozen with them.

Imports only code that exists on master before PR C.
"""

from __future__ import annotations

import dataclasses
from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.catalog import Work
from backend.domain.enums import CatalogSource, MatchStatus, MatchTier
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.services.missing_file_reconciliation_service import ReconciliationRepos
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

# One track: every file built with these tags is the same track by the MBID rule.
KISS = AudioMetadata(recording_mbid="rec-1", release_mbid="rel-1", duration_ms=54_040)


def fake_repos() -> ReconciliationRepos:
    """Fakes wired so works and masters see the library files' statuses."""
    files, works, masters = (
        FakeLibraryFileRepository(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
    )
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    return ReconciliationRepos(
        files=files,
        matches=FakeMatchRepository(),
        works=works,
        song_masters=masters,
        format_overrides=FakeFormatOverrideRepository(),
    )


def add_work(repos: ReconciliationRepos, work_id: str = "w1") -> str:
    repos.works.upsert(
        Work(
            id=work_id,
            title="Kiss",
            artist_id="a1",
            origin=CatalogSource.LOCAL,
            needs_enhancement=False,
        )
    )
    return work_id


def add_file(
    repos: ReconciliationRepos,
    path: str,
    *,
    missing: bool,
    work_id: str | None = "w1",
    audio: AudioMetadata = KISS,
    file_format: str = "flac",
    audio_hash: AudioHash | None = None,
) -> LibraryFile:
    """A row holding *audio*'s track, marked missing when asked."""
    row = repos.files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format=file_format,
            work_id=work_id,
            audio_hash=audio_hash,
            audio=dataclasses.replace(audio),
        )
    )
    if missing:
        repos.files.mark_missing(path)
    return row


def matched_identity(
    repos: ReconciliationRepos,
    identities: BroadcastTrackIdentityRepository,
    *file_ids: UUID,
) -> UUID:
    """An AUTO_MATCHED identity with one match per file in *file_ids*."""
    identity = identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=uuid4(),
            original_title="Kiss",
            normalized_title="kiss",
            normalized_signature=str(uuid4()),
            match_status=MatchStatus.AUTO_MATCHED,
            match_tier=MatchTier.LOCAL_FILE_FUZZY,
        )
    )
    for file_id in file_ids:
        repos.matches.create(
            Match(
                id=uuid4(),
                confidence_score=0.9,
                match_tier=MatchTier.LOCAL_FILE_FUZZY,
                identity_id=identity.id,
                library_file_id=file_id,
            )
        )
    return identity.id
