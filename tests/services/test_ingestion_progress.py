"""Unit tests for the progress-tracking hooks added to ingestion_service.

Covers:
- ``count_csv_rows`` (valid row count; decode error propagation).
- ``ingest_csv`` ``on_row_processed`` contract: attempt-zero 0, cadence
  every 100 rows, unconditional final fire.
- Counter/ingest parity: ``count_csv_rows`` and ``ingest_csv`` classify
  the same rows as "valid".
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from backend.services.ingestion_service import (
    CsvDecodeError,
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


def _make_csv(valid_rows: int, *, invalid_rows: int = 0) -> bytes:
    """Build a minimal KAZR-shaped CSV with N valid rows + M invalid rows."""
    lines = ["Station,Played,Artist,Title"]
    for i in range(valid_rows):
        # Unique artist+title so the normalized signature is unique per row
        # and every row really does count as one "event".
        lines.append(
            f"KAZR,2005-03-02 00:{i // 60:02d}:{i % 60:02d},Artist_{i},Title_{i}"
        )
    for i in range(invalid_rows):
        # Missing Artist/Title — _is_valid_ingest_row should reject.
        lines.append(f"KAZR,2005-03-02 00:00:{i:02d},,")
    return ("\r\n".join(lines) + "\r\n").encode()


def _fresh_repos() -> dict[str, object]:
    return {
        "playlist_repo": FakeBroadcastPlaylistRepository(),
        "broadcast_artist_repo": FakeBroadcastArtistRepository(),
        "track_identity_repo": FakeBroadcastTrackIdentityRepository(),
        "play_event_repo": FakeBroadcastPlayEventRepository(),
        "broadcast_day_repo": FakeBroadcastDayRepository(),
    }


class TestCountCsvRows:
    def test_counts_valid_rows_only(self) -> None:
        payload = _make_csv(valid_rows=42, invalid_rows=5)
        assert count_csv_rows(payload) == 42

    def test_empty_csv_returns_zero(self) -> None:
        assert count_csv_rows(b"Station,Played,Artist,Title\r\n") == 0

    def test_propagates_decode_error(self) -> None:
        # Random high bytes — matches test_ingestion_decode's garbage pattern.
        garbage = bytes(range(128, 256)) * 20
        with pytest.raises(CsvDecodeError):
            count_csv_rows(garbage)


class TestOnRowProcessedContract:
    def test_fires_attempt_zero_cadence_and_final(self) -> None:
        payload = _make_csv(valid_rows=250)
        observations: list[int] = []
        repos = _fresh_repos()

        ingest_csv(
            file_bytes=payload,
            file_name="cadence.csv",
            station_id=str(uuid4()),
            on_row_processed=observations.append,
            **repos,  # type: ignore[arg-type]
        )

        # Leading 0 = attempt-zero signal (fires once per ingest_csv call,
        # even on the first attempt). Then 100, 200 cadence ticks. Then 250
        # is the unconditional final fire.
        assert observations == [0, 100, 200, 250]

    def test_terminal_value_matches_row_count(self) -> None:
        """Parity invariant: final observation == rows_processed == count_csv_rows."""
        payload = _make_csv(valid_rows=137, invalid_rows=20)
        observations: list[int] = []
        repos = _fresh_repos()

        result = ingest_csv(
            file_bytes=payload,
            file_name="parity.csv",
            station_id=str(uuid4()),
            on_row_processed=observations.append,
            **repos,  # type: ignore[arg-type]
        )

        # Three independent paths must agree on how many rows are "valid".
        assert observations[-1] == 137
        assert result.rows_processed == 137
        assert count_csv_rows(payload) == 137

    def test_no_callback_does_not_crash(self) -> None:
        """Default behaviour (no callback) must be identical to pre-feature code."""
        payload = _make_csv(valid_rows=5)
        repos = _fresh_repos()
        result = ingest_csv(
            file_bytes=payload,
            file_name="nocallback.csv",
            station_id=str(uuid4()),
            **repos,  # type: ignore[arg-type]
        )
        assert result.rows_processed == 5
