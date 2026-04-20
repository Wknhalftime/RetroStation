"""Unit tests for the progress-tracking hooks added to ingestion_service.

Covers:
- ``count_csv_rows`` (valid row count; decode error propagation).
- ``ingest_csv`` ``on_row_processed`` contract: attempt-zero 0, cadence
  every ``_PROGRESS_REPORT_INTERVAL`` rows, and a trailing final emit
  only when the last cadence tick did not already land on the
  terminal count (avoids duplicate emits on exact multiples / 0).
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

    def test_short_row_with_missing_columns_returns_count_without_raising(
        self,
    ) -> None:
        """DictReader gives short rows a None-valued entry for missing
        columns; the extractor must discard those cleanly rather than
        crash on ``None.strip()``."""
        # Second data row is truncated — only has Station,Played,Artist (no Title).
        payload = (
            b"Station,Played,Artist,Title\r\n"
            b"KAZR,2005-03-02 00:01:00,Good_Artist,Good_Title\r\n"
            b"KAZR,2005-03-02 00:02:00,Orphan_Artist\r\n"
            b"KAZR,2005-03-02 00:03:00,Another_Good,Another_Title\r\n"
        )
        assert count_csv_rows(payload) == 2


class TestSkippedRowObservability:
    """`ingest_csv` must never silently drop rows: every malformed line
    is counted on ``IngestionResult.rows_skipped`` so operators can see
    the gap between "lines in the file" and "events ingested"."""

    def test_short_rows_are_counted_as_skipped(self) -> None:
        payload = (
            b"Station,Played,Artist,Title\r\n"
            b"KAZR,2005-03-02 00:01:00,Good_A,Good_T\r\n"
            b"KAZR,2005-03-02 00:02:00,Orphan\r\n"  # short row → None fields
            b"KAZR,2005-03-02 00:03:00,Good_B,Good_T2\r\n"
        )
        repos = _fresh_repos()
        result = ingest_csv(
            file_bytes=payload,
            file_name="shortrow.csv",
            station_id=str(uuid4()),
            **repos,  # type: ignore[arg-type]
        )
        assert result.rows_processed == 2
        assert result.rows_skipped == 1

    def test_rows_skipped_counts_all_invalid_rows(self) -> None:
        """Short, blank, and all-empty rows all count as skipped."""
        payload = (
            b"Station,Played,Artist,Title\r\n"
            b"KAZR,2005-03-02 00:01:00,Good_A,Good_T\r\n"
            b"KAZR,2005-03-02 00:02:00,,\r\n"  # blank artist+title
            b"KAZR,2005-03-02 00:03:00,Orphan\r\n"  # short row
            b",,,\r\n"  # all blank
            b"KAZR,2005-03-02 00:04:00,Good_B,Good_T2\r\n"
        )
        repos = _fresh_repos()
        result = ingest_csv(
            file_bytes=payload,
            file_name="mixed.csv",
            station_id=str(uuid4()),
            **repos,  # type: ignore[arg-type]
        )
        assert result.rows_processed == 2
        assert result.rows_skipped == 3

    def test_long_row_with_unquoted_comma_is_rejected_not_misaligned(self) -> None:
        """An unquoted comma inside a field makes DictReader think the row
        has extra columns — parking the overflow under a None key. We treat
        that as misaligned and skip, rather than silently ingest
        truncated Artist/Title values."""
        payload = (
            b"Station,Played,Artist,Title\r\n"
            b"KAZR,2005-03-02 00:01:00,Good,Good_Title\r\n"
            # Unquoted comma in what should be one "Artist" field:
            b"KAZR,2005-03-02 00:02:00,Foo, Jr.,Split_Title\r\n"
            b"KAZR,2005-03-02 00:03:00,Good2,Good_Title2\r\n"
        )
        repos = _fresh_repos()
        result = ingest_csv(
            file_bytes=payload,
            file_name="misaligned.csv",
            station_id=str(uuid4()),
            **repos,  # type: ignore[arg-type]
        )
        assert result.rows_processed == 2
        assert result.rows_skipped == 1
        # Parity: count_csv_rows agrees with the real loop.
        assert count_csv_rows(payload) == 2

    def test_rows_skipped_is_zero_for_clean_csv(self) -> None:
        """Happy path — no defensive bumps to rows_skipped."""
        payload = _make_csv(valid_rows=10)
        repos = _fresh_repos()
        result = ingest_csv(
            file_bytes=payload,
            file_name="clean.csv",
            station_id=str(uuid4()),
            **repos,  # type: ignore[arg-type]
        )
        assert result.rows_processed == 10
        assert result.rows_skipped == 0


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
        # even on the first attempt). Then 100 and 200 are cadence ticks.
        # 250 is the trailing final fire, emitted because the last cadence
        # tick (200) didn't already land on the terminal count. See
        # test_exact_multiple_of_interval_does_not_duplicate_final for the
        # complementary case where the trailing emit is suppressed.
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

    def test_exact_multiple_of_interval_does_not_duplicate_final(self) -> None:
        """When rows_processed % interval == 0, the cadence tick already
        reported the terminal value; the final fire must NOT duplicate it."""
        payload = _make_csv(valid_rows=200)
        observations: list[int] = []
        repos = _fresh_repos()

        ingest_csv(
            file_bytes=payload,
            file_name="exact.csv",
            station_id=str(uuid4()),
            on_row_processed=observations.append,
            **repos,  # type: ignore[arg-type]
        )

        # Attempt-zero, 100 cadence, 200 cadence. No trailing duplicate 200.
        assert observations == [0, 100, 200]

    def test_empty_csv_fires_only_attempt_zero(self) -> None:
        """No valid rows → no cadence ticks → final fire must be suppressed
        so the callback doesn't emit 0 twice."""
        payload = _make_csv(valid_rows=0)
        observations: list[int] = []
        repos = _fresh_repos()

        ingest_csv(
            file_bytes=payload,
            file_name="empty.csv",
            station_id=str(uuid4()),
            on_row_processed=observations.append,
            **repos,  # type: ignore[arg-type]
        )

        assert observations == [0]

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
