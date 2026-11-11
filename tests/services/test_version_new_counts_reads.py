"""What the artist pages' new-version counts read (D5; the artist-list follow-up to versions
phase 2b PR 3).

The marker needs a work's version texts and, for a seen text, the work's files. So the counts read
the given works' texts, then the files of only the works that have one: never every given work's
files, and never the line files, which only suggestions use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.domain.version_pages import VersionEntryRow, WorkFile
from backend.services.version_entry_service import new_counts

SEEN = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def text(work_id: str, version_text: str, seen_at: datetime | None) -> VersionEntryRow:
    return VersionEntryRow(
        work_id=work_id,
        version_text=version_text,
        plays=1,
        stations=("KISW",),
        first_seen=None,
        first_station=None,
        mapped_file_id=None,
        dismissed=False,
        seen_at=seen_at,
    )


def copy(work_id: str, version_type: str, indexed_at: datetime) -> WorkFile:
    return WorkFile(
        work_id=work_id,
        file_id=uuid4(),
        version_type=version_type,
        indexed_at=indexed_at,
        track_title=None,
        file_path=f"D:/music/{uuid4()}.flac",
    )


class RecordingReader:
    """The version pages' reader over rows held in memory; it records each read and its works."""

    def __init__(self, rows: list[VersionEntryRow], files: list[WorkFile]) -> None:
        self._rows = rows
        self._files = files
        self.reads: list[tuple[str, list[str] | None]] = []

    async def entry_rows(self, work_ids: list[str] | None) -> list[VersionEntryRow]:
        self.reads.append(("entry_rows", work_ids))
        return [r for r in self._rows if work_ids is None or r.work_id in work_ids]

    async def line_files(
        self, work_ids: list[str] | None
    ) -> dict[tuple[str, str], frozenset[UUID]]:
        self.reads.append(("line_files", work_ids))
        return {}

    async def work_files(self, work_ids: list[str]) -> dict[str, list[WorkFile]]:
        self.reads.append(("work_files", work_ids))
        found: dict[str, list[WorkFile]] = {}
        for f in self._files:
            if f.work_id in work_ids:
                found.setdefault(f.work_id, []).append(f)
        return found

    async def master(self, work_id: str) -> tuple[UUID | None, str | None]:
        raise AssertionError("the counts don't read a master")

    async def work_titles(self, work_ids: list[str]) -> dict[str, dict[str, object]]:
        raise AssertionError("the counts don't read titles")

    def files_read_for(self) -> set[str]:
        return {w for name, works in self.reads if name == "work_files" for w in works or []}

    def read_names(self) -> set[str]:
        return {name for name, _ in self.reads}


async def test_reads_files_only_for_works_with_a_text() -> None:
    """Of three works, only "answered" has a text: its seen live text has a later copy, so it
    is new. "quiet" holds a copy indexed later but no text; "bare" holds nothing."""
    reader = RecordingReader(
        rows=[text("answered", "live", SEEN)],
        files=[
            copy("answered", "live", SEEN + timedelta(days=1)),
            copy("quiet", "live", SEEN + timedelta(days=1)),
        ],
    )

    counts = await new_counts(reader, ["answered", "quiet", "bare"])

    assert counts == {"answered": 1}
    assert reader.reads[0] == ("entry_rows", ["answered", "quiet", "bare"])
    assert reader.files_read_for() == {"answered"}
    assert "line_files" not in reader.read_names()


async def test_no_text_reads_no_files() -> None:
    """Works with files but no version text: nothing is new, and no file is read."""
    reader = RecordingReader(rows=[], files=[copy("quiet", "live", SEEN + timedelta(days=1))])

    counts = await new_counts(reader, ["quiet", "bare"])

    assert counts == {}
    assert reader.files_read_for() == set()
    assert "line_files" not in reader.read_names()


async def test_no_works_reads_nothing() -> None:
    reader = RecordingReader(rows=[], files=[])

    assert await new_counts(reader, []) == {}
    assert reader.reads == []
