"""Tests for the M3U generator service.

All tests use in-memory fake repositories; no database required.

Which file plays for a play is the curation view ``play_file_resolution`` (spec D17), so the
fake resolution repository is a lookup table seeded per play: these tests cover what the
export does with a resolution. The resolution rules themselves (song master, format override,
the file's own work, matched statuses) are tested against the real view in
``tests/routers/test_m3u_export.py`` and ``tests/integration/test_play_file_resolution.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(
    *, file_path: str = "/music/track.flac", duration_ms: int | None = 301_000
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        audio=AudioMetadata(duration_ms=duration_ms),
    )


@dataclass
class _Station:
    """Fake repositories plus helpers that log a play and say what it resolves to."""

    settings: dict[str, str] | None = None
    identities: FakeBroadcastTrackIdentityRepository = field(
        default_factory=FakeBroadcastTrackIdentityRepository
    )
    resolutions: FakePlayFileResolutionRepository = field(
        default_factory=FakePlayFileResolutionRepository
    )
    files: FakeLibraryFileRepository = field(default_factory=FakeLibraryFileRepository)

    def add_file(self, library_file: LibraryFile) -> LibraryFile:
        self.files.upsert(library_file)
        return library_file

    def play(
        self,
        *,
        title: str = "Test Song",
        resolves_to: LibraryFile | None,
        played_at: datetime = NOON,
    ) -> BroadcastPlayEvent:
        """A play of a fresh matched identity that resolves to ``resolves_to`` (or to no
        file), with the final file's current status as the view would report it."""
        identity = BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=uuid4(),
            original_title=title,
            normalized_title=title.lower(),
            normalized_signature=f"artist:{title.lower()}:{uuid4()}",
            match_status=MatchStatus.AUTO_MATCHED,
            match_tier=MatchTier.NORMALIZATION,
        )
        self.identities.upsert(identity)
        event = BroadcastPlayEvent(
            id=uuid4(), identity_id=identity.id, playlist_id=uuid4(), played_at=played_at
        )
        self.resolutions.add(
            PlayFileResolution(
                play_event_id=event.id,
                file_id=None if resolves_to is None else resolves_to.id,
                file_status=None if resolves_to is None else resolves_to.file_status,
            )
        )
        return event

    def export(self, events: list[BroadcastPlayEvent]) -> str:
        return generate_m3u(
            events,
            M3uRepos(
                track_identities=self.identities,
                play_file_resolution=self.resolutions,
                library_files=self.files,
                user_settings=FakeUserSettingRepository(initial=self.settings),
            ),
        )


def _file_lines(m3u: str) -> list[str]:
    return [ln for ln in m3u.splitlines() if ln and not ln.startswith("#")]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBasicExport:
    def test_basic_export(self) -> None:
        """A play that resolves to a present file emits its EXTINF line and path."""
        station = _Station()
        library_file = station.add_file(
            _make_file(file_path="/music/Nirvana/01-smells.flac", duration_ms=301_000)
        )
        event = station.play(title="Smells Like Teen Spirit", resolves_to=library_file)

        result = station.export([event])

        assert result.startswith("#EXTM3U")
        assert "/music/Nirvana/01-smells.flac" in result
        assert "#EXTINF:301,Smells Like Teen Spirit" in result


class TestNavidromePathMapping:
    def test_navidrome_path_mapping(self) -> None:
        """local_path_prefix is replaced by navidrome_path_prefix in the output."""
        station = _Station(
            settings={
                "local_path_prefix": "/music",
                "navidrome_path_prefix": "/data/music",
            }
        )
        library_file = station.add_file(_make_file(file_path="/music/Nirvana/01.flac"))
        event = station.play(title="Come As You Are", resolves_to=library_file)

        result = station.export([event])

        assert "/data/music/Nirvana/01.flac" in result
        # Ensure the local prefix has been fully replaced (no bare /music/ lines)
        assert all(ln.startswith("/data/music/") for ln in _file_lines(result))


class TestUnresolvedPlaysSkipped:
    def test_a_play_that_resolves_to_no_file_is_skipped(self) -> None:
        """No override or master (D22), or an unmatched identity: header only."""
        station = _Station()
        event = station.play(title="Pending Song", resolves_to=None)

        lines = [ln for ln in station.export([event]).splitlines() if ln.strip()]
        assert lines == ["#EXTM3U"]

    def test_empty_playlist_returns_header_only(self) -> None:
        """A playlist with no events produces only the #EXTM3U header."""
        lines = [ln for ln in _Station().export([]).splitlines() if ln.strip()]
        assert lines == ["#EXTM3U"]


class TestDurationHandling:
    def test_no_duration_emits_minus_one(self) -> None:
        """Files with no duration_ms emit -1 in EXTINF."""
        station = _Station()
        library_file = station.add_file(_make_file(file_path="/music/nodur.mp3", duration_ms=None))
        event = station.play(title="No Duration", resolves_to=library_file)

        assert "#EXTINF:-1,No Duration" in station.export([event])


class TestMissingFiles:
    @pytest.mark.parametrize("status", [FileStatus.MISSING, FileStatus.DELETED])
    def test_a_file_that_is_not_present_is_not_written(self, status: FileStatus) -> None:
        """D21: only a ``present`` final file is written; a missing or deleted one would be
        a dead entry in the player."""
        station = _Station()
        gone = _make_file(file_path="/music/gone.flac")
        gone.file_status = status
        station.add_file(gone)
        event = station.play(title="Ezekiel 25:17", resolves_to=gone)

        assert station.export([event]) == "#EXTM3U\n"


class TestOrder:
    def test_plays_are_written_in_the_order_given(self) -> None:
        """The caller owns the order: a station-day comes in ``station_day_plays`` position
        order (D19), which is not always ``played_at`` order within a tie, so the export
        must not re-sort."""
        station = _Station()
        later = station.add_file(_make_file(file_path="/music/later.flac"))
        earlier = station.add_file(_make_file(file_path="/music/earlier.flac"))
        events = [
            station.play(resolves_to=later, played_at=NOON.replace(hour=13)),
            station.play(resolves_to=earlier, played_at=NOON),
        ]

        assert _file_lines(station.export(events)) == [
            "/music/later.flac",
            "/music/earlier.flac",
        ]
