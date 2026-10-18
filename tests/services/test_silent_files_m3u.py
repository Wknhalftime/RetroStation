"""Acceptance tests: an M3U export leaves out a silent file (S7, D111), on fakes.

The export reads which file plays from curation's resolution (D17), which also reports whether
the final file's audio is silent. A silent play gets no line, as a file that is not present
gets none (D21); its neighbours keep their lines and their order. ``audio_silent`` is passed by
keyword, so before the build each test fails on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.broadcast import BroadcastPlayEvent, BroadcastTrackIdentity
from backend.domain.curation import PlayFileResolution
from backend.domain.enums import EnrichmentStatus, FileStatus, MatchStatus, MatchTier
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.m3u_generator_service import M3uRepos, generate_m3u
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.play_file_resolution import FakePlayFileResolutionRepository
from tests.fakes.user_settings import FakeUserSettingRepository

NOON = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def export(plays: list[tuple[str, bool]]) -> list[str]:
    """Export one present file per (path, silent) pair, in order; the M3U's file lines."""
    identities = FakeBroadcastTrackIdentityRepository()
    resolutions = FakePlayFileResolutionRepository()
    files = FakeLibraryFileRepository()
    events: list[BroadcastPlayEvent] = []
    for path, silent in plays:
        library_file = LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(duration_ms=200_000),
        )
        files.upsert(library_file)
        identity = BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=uuid4(),
            original_title=path,
            normalized_title=path,
            normalized_signature=f"artist:{path}:{uuid4()}",
            match_status=MatchStatus.AUTO_MATCHED,
            match_tier=MatchTier.NORMALIZATION,
        )
        identities.upsert(identity)
        event = BroadcastPlayEvent(
            id=uuid4(), identity_id=identity.id, playlist_id=uuid4(), played_at=NOON
        )
        resolutions.add(
            PlayFileResolution(event.id, library_file.id, FileStatus.PRESENT, audio_silent=silent)
        )
        events.append(event)
    repos = M3uRepos(
        track_identities=identities,
        play_file_resolution=resolutions,
        library_files=files,
        user_settings=FakeUserSettingRepository(),
    )
    return [line for line in generate_m3u(events, repos).splitlines() if line[:1] not in "#"]


def test_a_play_whose_audio_is_silent_is_not_written() -> None:
    plays = [("/m/a.flac", False), ("/m/silent.flac", True), ("/m/b.flac", False)]
    assert export(plays) == ["/m/a.flac", "/m/b.flac"]


def test_a_resolution_is_not_silent_unless_it_says_so() -> None:
    """The field defaults to false, so every existing reader and fake keeps its meaning."""
    assert PlayFileResolution(uuid4(), uuid4(), FileStatus.PRESENT).audio_silent is False
