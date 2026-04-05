"""Verify every fake is a concrete subclass of its ABC (raises TypeError at import if not)."""


def test_station_fake_is_concrete() -> None:
    from tests.fakes.stations import FakeStationRepository
    assert FakeStationRepository() is not None

def test_playlist_fake_is_concrete() -> None:
    from tests.fakes.playlists import FakePlaylistRepository
    assert FakePlaylistRepository() is not None

def test_broadcast_day_fake_is_concrete() -> None:
    from tests.fakes.broadcast_days import FakeBroadcastDayRepository
    assert FakeBroadcastDayRepository() is not None

def test_log_artist_fake_is_concrete() -> None:
    from tests.fakes.log_artists import FakeLogArtistRepository
    assert FakeLogArtistRepository() is not None

def test_log_identity_fake_is_concrete() -> None:
    from tests.fakes.log_identities import FakeLogIdentityRepository
    assert FakeLogIdentityRepository() is not None

def test_log_event_fake_is_concrete() -> None:
    from tests.fakes.log_events import FakeLogEventRepository
    assert FakeLogEventRepository() is not None

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

def test_global_mapping_rule_fake_is_concrete() -> None:
    from tests.fakes.global_mapping_rules import FakeGlobalMappingRuleRepository
    assert FakeGlobalMappingRuleRepository() is not None

def test_mb_cache_fake_is_concrete() -> None:
    from tests.fakes.mb_cache import FakeMbCacheRepository
    assert FakeMbCacheRepository() is not None

def test_progress_tracking_fake_is_concrete() -> None:
    from tests.fakes.progress_tracking import FakeProgressTrackingRepository
    assert FakeProgressTrackingRepository() is not None

def test_settings_fake_is_concrete() -> None:
    from tests.fakes.settings import FakeSettingsRepository
    assert FakeSettingsRepository() is not None

def test_library_folder_fake_is_concrete() -> None:
    from tests.fakes.library_folders import FakeLibraryFolderRepository
    assert FakeLibraryFolderRepository() is not None
