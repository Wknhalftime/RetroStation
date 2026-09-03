"""Unit tests for how the ingestion task surfaces rejections to the UI.

Two distinct signals, both previously invisible to the operator:

- A header mismatch is a hard FAILED with an ``error_code`` the frontend
  can key on, instead of a COMPLETED task reporting zero events.
- Row-level skips ride along on a COMPLETED task as a ``warning`` code
  plus a reason breakdown, matching the existing ``progress_data.warning``
  convention used by the library scan task.
"""
from __future__ import annotations

from backend.services.ingestion_service import (
    SKIP_BLANK_REQUIRED_FIELD,
    SKIP_EXTRA_FIELDS,
    CsvDecodeError,
    CsvSchemaError,
    DuplicatePlaylistError,
    IngestionResult,
)
from backend.tasks.ingestion_tasks import (
    _build_completed_progress,
    _classify_error_code,
)


class TestCompletedProgressWarning:
    def test_clean_run_carries_no_warning(self) -> None:
        result = IngestionResult(playlist_id="p1", rows_processed=10)
        payload = _build_completed_progress(result, total=10, filename="a.csv")
        assert "warning" not in payload

    def test_skipped_rows_set_the_warning_code(self) -> None:
        result = IngestionResult(
            playlist_id="p1",
            rows_processed=8,
            rows_skipped=2,
            skip_reasons={SKIP_BLANK_REQUIRED_FIELD: 2},
        )
        payload = _build_completed_progress(result, total=10, filename="a.csv")
        assert payload["warning"] == "rows_skipped"

    def test_skip_reasons_are_passed_through_for_display(self) -> None:
        result = IngestionResult(
            playlist_id="p1",
            rows_processed=8,
            rows_skipped=2,
            skip_reasons={SKIP_EXTRA_FIELDS: 1, SKIP_BLANK_REQUIRED_FIELD: 1},
        )
        payload = _build_completed_progress(result, total=10, filename="a.csv")
        assert payload["skip_reasons"] == {
            SKIP_EXTRA_FIELDS: 1,
            SKIP_BLANK_REQUIRED_FIELD: 1,
        }

    def test_existing_counts_are_unchanged(self) -> None:
        """Regression guard: the warning is additive, not a rewrite."""
        result = IngestionResult(
            playlist_id="p1", rows_processed=8, rows_skipped=2, events_created=8
        )
        payload = _build_completed_progress(result, total=10, filename="a.csv")
        assert payload["processed"] == 8
        assert payload["skipped"] == 2
        assert payload["events_created"] == 8
        assert payload["filename"] == "a.csv"


class TestErrorCodeClassification:
    def test_schema_error_is_classified(self) -> None:
        assert _classify_error_code(CsvSchemaError("bad header")) == "csv_schema_mismatch"

    def test_duplicate_is_still_classified(self) -> None:
        assert _classify_error_code(DuplicatePlaylistError("dupe")) == "duplicate_playlist"

    def test_decode_error_is_classified(self) -> None:
        assert _classify_error_code(CsvDecodeError("bad bytes")) == "csv_decode_failed"

    def test_unknown_error_has_no_code(self) -> None:
        assert _classify_error_code(RuntimeError("boom")) is None
