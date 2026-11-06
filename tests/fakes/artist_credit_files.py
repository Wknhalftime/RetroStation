"""In-memory fake of the files' artist columns (artist model phase 2a, SPEC S1, S3, S10, S14)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from backend.domain.catalog import holds_several_mbids
from backend.domain.library import ArtistCreditFile, CreditEntry, CreditGroup
from backend.repositories.artist_credit_files import ArtistCreditFilesRepository


@dataclass
class FakeCreditFile:
    """One ``library_files`` row, as far as the port cares."""

    id: UUID
    file_path: str = "/music/a.flac"
    format: str = "flac"
    raw_metadata: dict[str, Any] | None = None
    recording_mbid: str | None = None
    artist_mbids: tuple[str, ...] | None = None
    credit: tuple[CreditEntry, ...] | None = None
    present: bool = True


class FakeArtistCreditFilesRepository(ArtistCreditFilesRepository):
    def __init__(self) -> None:
        self.files: list[FakeCreditFile] = []

    def add(self, **fields: Any) -> FakeCreditFile:
        file = FakeCreditFile(id=uuid4(), **fields)
        self.files.append(file)
        return file

    def list_to_fill(self, after_id: UUID | None, limit: int) -> list[ArtistCreditFile]:
        due = sorted(
            (
                f
                for f in self.files
                if f.raw_metadata is not None
                and (after_id is None or f.id > after_id)
                and (f.artist_mbids is None or any(e.name is None for e in f.credit or ()))
            ),
            key=lambda f: f.id,
        )
        return [
            ArtistCreditFile(
                id=f.id,
                file_path=f.file_path,
                format=f.format,
                raw_metadata=f.raw_metadata or {},
                recording_mbid=f.recording_mbid,
            )
            for f in due[:limit]
        ]

    def write_credit(
        self,
        file_id: UUID,
        artist_mbids: Sequence[str],
        credit: Sequence[CreditEntry] | None,
    ) -> None:
        for file in self.files:
            if file.id == file_id:
                file.artist_mbids = tuple(artist_mbids)
                file.credit = None if credit is None else tuple(credit)

    def list_credit_groups(self) -> list[CreditGroup]:
        groups = Counter(
            (entry.mbid, entry.name)
            for f in self.files
            if f.present and holds_several_mbids(f.artist_mbids or ())
            for entry in f.credit or ()
        )
        return [
            CreditGroup(mbid=mbid, name=name, files=files) for (mbid, name), files in groups.items()
        ]

    def list_tag_spellings(self) -> set[tuple[str, str]]:
        return {
            (entry.mbid, entry.name)
            for f in self.files
            for entry in f.credit or ()
            if entry.name is not None
        }
