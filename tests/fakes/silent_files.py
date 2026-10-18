"""The in-memory Library silent-files read (S6), beside the Pg adapter."""

from collections.abc import Iterable

from backend.domain.streaming import SilentFileEntry
from backend.repositories.silent_files import SilentFileReportRepository


class FakeSilentFileReportRepository(SilentFileReportRepository):
    """Answers every entry a test gives it, with or without a reason: the Pg adapter
    pre-filters, but the port promises only "at least every file with a reason", so the
    service must keep the files with a reason itself."""

    def __init__(self, entries: Iterable[SilentFileEntry] = ()) -> None:
        self.entries = list(entries)

    def candidates(self) -> tuple[SilentFileEntry, ...]:
        return tuple(self.entries)
