"""The Library Silent Files report (D111, S6; issue #146): which present files are silent or
nearly silent, so they can be found and replaced. Read-only."""

from __future__ import annotations

from backend.domain.streaming import SilenceReason, SilentFileEntry, SilentFileReport
from backend.repositories.silent_files import SilentFileReportRepository


def _order(entry: SilentFileEntry) -> tuple[bool, str]:
    """Silent files first, then the nearly silent; each group in path order."""
    return (SilenceReason.SILENT not in entry.reasons(), entry.path)


def silent_file_report(report: SilentFileReportRepository) -> SilentFileReport:
    """The files the report lists, each with at least one reason."""
    listed = sorted((e for e in report.candidates() if e.reasons()), key=_order)
    return SilentFileReport(tuple(listed))
