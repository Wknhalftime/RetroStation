"""Tests for the M3U generator service.

All tests use in-memory fake repositories; no database required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.domain.broadcast import BroadcastPlayEvent, BroadcastTrackIdentity
from backend.domain.catalog import Recording
from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, SelectionMethod
from backend.domain.library import LibraryFile
from backend.domain.matching import Match
from backend.services.m3u_generator_service import generate_m3u
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.play_events import FakeBroadcastPlayEventRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.user_settings import FakeUserSettingRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_identity(
    *,
    title: str = "Test Song",
    match_status: MatchStatus = MatchStatus.AUTO_MATCHED,
) -> BroadcastTrackIdentity:
    artist_id = uuid4()
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist_id,
        original_title=title,
        normalized_title=title.lower(),
        normalized_signature=f"artist:{title.lower()}",
        match_status=match_status,
        match_tier=MatchTier.NORMALIZATION,
    )


def _make_event(*, identity: BroadcastTrackIdentity, playlist_id: object) -> BroadcastPlayEvent:
    from uuid import UUID
    return BroadcastPlayEvent(
        id=uuid4(),
        identity_id=identity.id,
        playlist_id=UUID(str(playlist_id)),
        played_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


def _make_file(*, file_path: str = "/music/track.flac", duration_ms: int | None = 301_000) -> LibraryFile:
    from backend.domain.library import AudioMetadata
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash=uuid4().hex,
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        audio=AudioMetadata(duration_ms=duration_ms),
    )


def _make_match(*, identity: BroadcastTrackIdentity, library_file: LibraryFile) -> Match:
    return Match(
        id=uuid4(),
        identity_id=identity.id,
        library_file_id=library_file.id,
        confidence_score=0.99,
        match_tier=MatchTier.NORMALIZATION,
    )


def _build_repos(
    *,
    settings: dict[str, str] | None = None,
) -> tuple[
    FakeBroadcastPlayEventRepository,
    FakeBroadcastTrackIdentityRepository,
    FakeMatchRepository,
    FakeLibraryFileRepository,
    FakeRecordingRepository,
    FakeSongMasterRepository,
    FakeFormatOverrideRepository,
    FakeUserSettingRepository,
]:
    return (
        FakeBroadcastPlayEventRepository(),
        FakeBroadcastTrackIdentityRepository(),
        FakeMatchRepository(),
        FakeLibraryFileRepository(),
        FakeRecordingRepository(),
        FakeSongMasterRepository(),
        FakeFormatOverrideRepository(),
        FakeUserSettingRepository(initial=settings),
    )


def _call_generate(
    playlist_id: object,
    *,
    event_repo: FakeBroadcastPlayEventRepository,
    identity_repo: FakeBroadcastTrackIdentityRepository,
    match_repo: FakeMatchRepository,
    file_repo: FakeLibraryFileRepository,
    recording_repo: FakeRecordingRepository,
    master_repo: FakeSongMasterRepository,
    override_repo: FakeFormatOverrideRepository,
    settings_repo: FakeUserSettingRepository,
    station_format: str | None = None,
) -> str:
    from uuid import UUID
    events = event_repo.get_by_playlist(UUID(str(playlist_id)))
    return generate_m3u(
        events=events,
        identity_repo=identity_repo,
        match_repo=match_repo,
        file_repo=file_repo,
        recording_repo=recording_repo,
        master_repo=master_repo,
        override_repo=override_repo,
        settings_repo=settings_repo,
        station_format=station_format,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBasicExport:
    def test_basic_export(self) -> None:
        """Matched event emits the file path in the output."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos()

        identity = _make_identity(title="Smells Like Teen Spirit")
        identity_repo.upsert(identity)

        library_file = _make_file(file_path="/music/Nirvana/01-smells.flac", duration_ms=301_000)
        file_repo.upsert(library_file)

        match = _make_match(identity=identity, library_file=library_file)
        match_repo.create(match)

        event = _make_event(identity=identity, playlist_id=playlist_id)
        event_repo.create(event)

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
        )

        assert result.startswith("#EXTM3U")
        assert "/music/Nirvana/01-smells.flac" in result
        assert "#EXTINF:301,Smells Like Teen Spirit" in result


class TestNavidromePathMapping:
    def test_navidrome_path_mapping(self) -> None:
        """local_path_prefix is replaced by navidrome_path_prefix in the output."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos(
            settings={
                "local_path_prefix": "/music",
                "navidrome_path_prefix": "/data/music",
            }
        )

        identity = _make_identity(title="Come As You Are")
        identity_repo.upsert(identity)

        library_file = _make_file(file_path="/music/Nirvana/01.flac")
        file_repo.upsert(library_file)

        match_repo.create(_make_match(identity=identity, library_file=library_file))
        event_repo.create(_make_event(identity=identity, playlist_id=playlist_id))

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
        )

        assert "/data/music/Nirvana/01.flac" in result
        # Ensure the local prefix has been fully replaced (no bare /music/ lines)
        file_lines = [ln for ln in result.splitlines() if ln and not ln.startswith("#")]
        assert all(ln.startswith("/data/music/") for ln in file_lines)


class TestSongMasterOverride:
    def test_song_master_overrides_direct_match(self) -> None:
        """When a song_master exists for the work, its preferred_file_id wins over
        the direct match file."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos()

        identity = _make_identity(title="Track A")
        identity_repo.upsert(identity)

        file_a = _make_file(file_path="/music/file_a.flac")
        file_b = _make_file(file_path="/music/file_b.flac")
        file_repo.upsert(file_a)
        file_repo.upsert(file_b)

        # Link file_a to a recording that belongs to a work
        work_id = "work-mbid-001"
        recording = Recording(
            id="rec-mbid-001",
            title="Track A",
            work_id=work_id,
            duration_ms=240_000,
        )
        recording_repo.upsert(recording)
        file_a.recording_id = recording.id  # simulate enrichment link

        match_repo.create(_make_match(identity=identity, library_file=file_a))
        event_repo.create(_make_event(identity=identity, playlist_id=playlist_id))

        # Song master prefers file_b
        master = SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file_b.id,
            selection_method=SelectionMethod.AUTO,
        )
        master_repo.upsert(master)

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
        )

        assert "/music/file_b.flac" in result
        assert "/music/file_a.flac" not in result


class TestFormatOverrideWins:
    def test_format_override_wins_over_song_master(self) -> None:
        """format_override for the matching station_format beats song_master."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos()

        identity = _make_identity(title="Radio Hit")
        identity_repo.upsert(identity)

        file_a = _make_file(file_path="/music/file_a.flac")
        file_b = _make_file(file_path="/music/file_b.flac")
        file_c = _make_file(file_path="/music/file_c.flac")
        for f in (file_a, file_b, file_c):
            file_repo.upsert(f)

        work_id = "work-mbid-chr"
        recording = Recording(
            id="rec-mbid-chr",
            title="Radio Hit",
            work_id=work_id,
            duration_ms=180_000,
        )
        recording_repo.upsert(recording)
        file_a.recording_id = recording.id

        match_repo.create(_make_match(identity=identity, library_file=file_a))
        event_repo.create(_make_event(identity=identity, playlist_id=playlist_id))

        # song_master → file_b
        master_repo.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work_id,
                preferred_file_id=file_b.id,
                selection_method=SelectionMethod.AUTO,
            )
        )

        # format_override CHR → file_c
        override_repo.create(
            FormatOverride(
                id=uuid4(),
                work_id=work_id,
                format_name="CHR",
                preferred_file_id=file_c.id,
            )
        )

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
            station_format="CHR",
        )

        assert "/music/file_c.flac" in result
        assert "/music/file_b.flac" not in result
        assert "/music/file_a.flac" not in result


class TestUnmatchedEventsSkipped:
    @pytest.mark.parametrize(
        "match_status",
        [
            MatchStatus.PENDING,
            MatchStatus.NEEDS_REVIEW,
            MatchStatus.AUTO_REJECTED,
            MatchStatus.MANUAL_REJECTED,
        ],
    )
    def test_unmatched_events_skipped(self, match_status: MatchStatus) -> None:
        """Events with non-matched identities produce only the #EXTM3U header."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos()

        identity = _make_identity(title="Pending Song", match_status=match_status)
        identity_repo.upsert(identity)

        library_file = _make_file(file_path="/music/track.flac")
        file_repo.upsert(library_file)
        match_repo.create(_make_match(identity=identity, library_file=library_file))
        event_repo.create(_make_event(identity=identity, playlist_id=playlist_id))

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
        )

        lines = [ln for ln in result.splitlines() if ln.strip()]
        assert lines == ["#EXTM3U"]

    def test_empty_playlist_returns_header_only(self) -> None:
        """A playlist with no events produces only the #EXTM3U header."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos()

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
        )

        lines = [ln for ln in result.splitlines() if ln.strip()]
        assert lines == ["#EXTM3U"]


class TestDurationHandling:
    def test_no_duration_emits_minus_one(self) -> None:
        """Files with no duration_ms emit -1 in EXTINF."""
        playlist_id = uuid4()
        (
            event_repo, identity_repo, match_repo,
            file_repo, recording_repo,
            master_repo, override_repo, settings_repo,
        ) = _build_repos()

        identity = _make_identity(title="No Duration")
        identity_repo.upsert(identity)
        library_file = _make_file(file_path="/music/nodur.mp3", duration_ms=None)
        file_repo.upsert(library_file)
        match_repo.create(_make_match(identity=identity, library_file=library_file))
        event_repo.create(_make_event(identity=identity, playlist_id=playlist_id))

        result = _call_generate(
            playlist_id,
            event_repo=event_repo,
            identity_repo=identity_repo,
            match_repo=match_repo,
            file_repo=file_repo,
            recording_repo=recording_repo,
            master_repo=master_repo,
            override_repo=override_repo,
            settings_repo=settings_repo,
        )

        assert "#EXTINF:-1,No Duration" in result
