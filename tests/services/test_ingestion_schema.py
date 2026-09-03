"""Unit tests for CSV header validation and skip-reason reporting.

Regression cover for the "upload succeeded, 0 events" failure mode: a log
exported with a different column layout (e.g. a split ``Log Date`` /
``Time Played`` pair instead of a single ``Played``) had every row silently
rejected, and the task still reported COMPLETED.

The ingester must instead:
- Raise :class:`CsvSchemaError` naming the missing columns, before any
  playlist row is written.
- Classify per-row rejections so a partially-bad file can explain which
  rows it dropped and why.
"""
from __future__ import annotations

import hashlib

import pytest

from backend.services.ingestion_service import (
    CsvSchemaError,
    IngestionError,
    count_csv_rows,
    ingest_csv,
)
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_days import FakeBroadcastDayRepository
from tests.fakes.broadcast_play_events import FakeBroadcastPlayEventRepository
from tests.fakes.broadcast_playlists import FakeBroadcastPlaylistRepository
from tests.fakes.broadcast_track_identities import (
    FakeBroadcastTrackIdentityRepository,
)

# The exact header of the real-world export that triggered the bug: the
# timestamp is split across two columns, so "Played" is absent.
_SPLIT_TIMESTAMP_CSV = (
    b"Station,Log Date,Time Played,Artist,Title,Release Year,Grc\r\n"
    b"KSTZ-FM,09/01/2001,12:00:00 AM,MATCHBOX TWENTY,Bent,2000,G\r\n"
)

_GOOD_CSV = (
    b"Station,Played,Artist,Title,Release Year,Grc\r\n"
    b"KAZR-FM,2005-03-02 00:01:00,OZZY OSBOURNE,Perry Mason,1995,G\r\n"
)


def _fresh_repos() -> dict[str, object]:
    return {
        "playlist_repo": FakeBroadcastPlaylistRepository(),
        "broadcast_artist_repo": FakeBroadcastArtistRepository(),
        "track_identity_repo": FakeBroadcastTrackIdentityRepository(),
        "play_event_repo": FakeBroadcastPlayEventRepository(),
        "broadcast_day_repo": FakeBroadcastDayRepository(),
    }


def _ingest(payload: bytes, station_id: str = "") -> object:
    repos = _fresh_repos()
    return ingest_csv(
        file_bytes=payload,
        file_name="test.csv",
        station_id=station_id,
        **repos,  # type: ignore[arg-type]
    )


class TestHeaderValidation:
    def test_count_rejects_missing_played_column(self) -> None:
        with pytest.raises(CsvSchemaError):
            count_csv_rows(_SPLIT_TIMESTAMP_CSV)

    def test_ingest_rejects_missing_played_column(self) -> None:
        with pytest.raises(CsvSchemaError):
            _ingest(_SPLIT_TIMESTAMP_CSV)

    def test_error_names_the_missing_column(self) -> None:
        with pytest.raises(CsvSchemaError) as exc_info:
            count_csv_rows(_SPLIT_TIMESTAMP_CSV)
        message = str(exc_info.value)
        assert "Played" in message

    def test_error_lists_the_columns_actually_found(self) -> None:
        """The operator needs to see their own header to spot the mismatch."""
        with pytest.raises(CsvSchemaError) as exc_info:
            count_csv_rows(_SPLIT_TIMESTAMP_CSV)
        message = str(exc_info.value)
        assert "Log Date" in message
        assert "Time Played" in message

    def test_error_lists_every_missing_column(self) -> None:
        payload = b"Station,Played,Release Year\r\nKAZR,2005-03-02 00:01:00,1995\r\n"
        with pytest.raises(CsvSchemaError) as exc_info:
            count_csv_rows(payload)
        message = str(exc_info.value)
        assert "Artist" in message
        assert "Title" in message

    def test_no_playlist_row_is_created_when_header_is_rejected(self) -> None:
        """A rejected file must not leave an orphan playlist behind."""
        repos = _fresh_repos()
        playlist_repo = repos["playlist_repo"]
        assert isinstance(playlist_repo, FakeBroadcastPlaylistRepository)
        with pytest.raises(CsvSchemaError):
            ingest_csv(
                file_bytes=_SPLIT_TIMESTAMP_CSV,
                file_name="test.csv",
                station_id="",
                **repos,  # type: ignore[arg-type]
            )
        content_hash = hashlib.sha256(_SPLIT_TIMESTAMP_CSV).hexdigest()
        assert playlist_repo.get_by_content_hash(content_hash) is None

    def test_empty_file_is_rejected_as_schema_error(self) -> None:
        with pytest.raises(CsvSchemaError):
            count_csv_rows(b"")

    def test_csv_schema_error_is_an_ingestion_error(self) -> None:
        """Callers catching the base class must keep catching this one."""
        assert issubclass(CsvSchemaError, IngestionError)

    def test_valid_header_is_accepted(self) -> None:
        assert count_csv_rows(_GOOD_CSV) == 1

    def test_header_only_file_is_valid_with_zero_rows(self) -> None:
        assert count_csv_rows(b"Station,Played,Artist,Title\r\n") == 0


class TestSkipReasons:
    def test_blank_required_field_is_counted_with_a_reason(self) -> None:
        payload = (
            b"Station,Played,Artist,Title\r\n"
            b"KAZR,2005-03-02 00:01:00,OZZY OSBOURNE,Perry Mason\r\n"
            b"KAZR,2005-03-02 00:02:00,,Missing Artist\r\n"
        )
        result = _ingest(payload)
        assert result.rows_processed == 1  # type: ignore[attr-defined]
        assert result.rows_skipped == 1  # type: ignore[attr-defined]
        assert result.skip_reasons == {"blank_required_field": 1}  # type: ignore[attr-defined]

    def test_extra_fields_row_is_counted_with_a_reason(self) -> None:
        """An unquoted comma in a title makes the row longer than the header."""
        payload = (
            b"Station,Played,Artist,Title\r\n"
            b"KAZR,2005-03-02 00:01:00,OZZY OSBOURNE,Perry Mason\r\n"
            b"KAZR,2005-03-02 00:02:00,BAND,Hello, World\r\n"
        )
        result = _ingest(payload)
        assert result.rows_processed == 1  # type: ignore[attr-defined]
        assert result.skip_reasons == {"extra_fields": 1}  # type: ignore[attr-defined]

    def test_clean_file_reports_no_skip_reasons(self) -> None:
        result = _ingest(_GOOD_CSV)
        assert result.rows_skipped == 0  # type: ignore[attr-defined]
        assert result.skip_reasons == {}  # type: ignore[attr-defined]
