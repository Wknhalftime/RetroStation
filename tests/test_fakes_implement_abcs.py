"""Verify every fake is a concrete subclass of its ABC (raises TypeError at import if not)."""


def test_station_fake_is_concrete() -> None:
    from tests.fakes.stations import FakeBroadcastStationRepository
    assert FakeBroadcastStationRepository() is not None

def test_playlist_fake_is_concrete() -> None:
    from tests.fakes.playlists import FakeBroadcastPlaylistRepository
    assert FakeBroadcastPlaylistRepository() is not None

def test_broadcast_day_fake_is_concrete() -> None:
    from tests.fakes.broadcast_days import FakeBroadcastDayRepository
    assert FakeBroadcastDayRepository() is not None

def test_broadcast_artist_fake_is_concrete() -> None:
    from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
    assert FakeBroadcastArtistRepository() is not None

def test_track_identity_fake_is_concrete() -> None:
    from tests.fakes.track_identities import FakeBroadcastTrackIdentityRepository
    assert FakeBroadcastTrackIdentityRepository() is not None

def test_play_event_fake_is_concrete() -> None:
    from tests.fakes.play_events import FakeBroadcastPlayEventRepository
    assert FakeBroadcastPlayEventRepository() is not None

def test_artist_fake_is_concrete() -> None:
    from tests.fakes.artists import FakeArtistRepository
    assert FakeArtistRepository() is not None

def test_work_fake_is_concrete() -> None:
    from tests.fakes.works import FakeWorkRepository
    assert FakeWorkRepository() is not None

def test_recording_fake_is_concrete() -> None:
    from tests.fakes.recordings import FakeRecordingRepository
    assert FakeRecordingRepository() is not None

def test_library_file_fake_is_concrete() -> None:
    from tests.fakes.library_files import FakeLibraryFileRepository
    assert FakeLibraryFileRepository() is not None

def test_library_quarantine_fake_is_concrete() -> None:
    from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository
    assert FakeLibraryQuarantineRepository() is not None

def test_match_fake_is_concrete() -> None:
    from tests.fakes.matches import FakeMatchRepository
    assert FakeMatchRepository() is not None

def test_song_master_fake_is_concrete() -> None:
    from tests.fakes.song_masters import FakeSongMasterRepository
    assert FakeSongMasterRepository() is not None

def test_format_override_fake_is_concrete() -> None:
    from tests.fakes.format_overrides import FakeFormatOverrideRepository
    assert FakeFormatOverrideRepository() is not None

def test_mapping_rule_fake_is_concrete() -> None:
    from tests.fakes.mapping_rules import FakeMappingRuleRepository
    assert FakeMappingRuleRepository() is not None

def test_mb_cache_fake_is_concrete() -> None:
    from tests.fakes.mb_cache import FakeMusicBrainzCacheRepository
    assert FakeMusicBrainzCacheRepository() is not None

def test_progress_tracking_fake_is_concrete() -> None:
    from tests.fakes.progress_tracking import FakeTaskProgressRepository
    assert FakeTaskProgressRepository() is not None

def test_settings_fake_is_concrete() -> None:
    from tests.fakes.user_settings import FakeUserSettingRepository
    assert FakeUserSettingRepository() is not None

def test_library_folder_fake_is_concrete() -> None:
    from tests.fakes.library_folders import FakeLibraryFolderRepository
    assert FakeLibraryFolderRepository() is not None

def test_system_log_fake_is_concrete() -> None:
    from tests.fakes.system_logs import FakeSystemLogRepository
    assert FakeSystemLogRepository() is not None
