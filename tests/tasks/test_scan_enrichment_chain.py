"""Test that library_scan_task chains into enrichment on completion."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestScanEnrichmentChain:
    @patch("backend.tasks.library_tasks.psycopg")
    @patch("backend.tasks.library_tasks.PgProgressTrackingRepository")
    def test_chains_enrichment_when_files_written(
        self,
        mock_progress_repo_cls: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_progress_repo_cls.return_value = MagicMock()

        with patch("backend.tasks.library_tasks._run_scan") as mock_run:
            mock_run.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})
            with patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task"
            ) as mock_enrich:
                from backend.tasks.library_tasks import library_scan_task

                library_scan_task.call_local("/music")
                mock_enrich.assert_called_once()

    @patch("backend.tasks.library_tasks.psycopg")
    @patch("backend.tasks.library_tasks.PgProgressTrackingRepository")
    def test_no_enrichment_when_zero_files(
        self,
        mock_progress_repo_cls: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_progress_repo_cls.return_value = MagicMock()

        with patch("backend.tasks.library_tasks._run_scan") as mock_run:
            mock_run.return_value = (0, 0, {"processed": 0, "total": 0, "current_path": ""})
            with patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task"
            ) as mock_enrich:
                from backend.tasks.library_tasks import library_scan_task

                library_scan_task.call_local("/empty")
                mock_enrich.assert_not_called()
