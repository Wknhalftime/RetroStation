"""In-memory fake of the sweep's read of ``library_files`` (artist model phase 2a, SPEC S14)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from backend.domain.library import ArtistTagGroup
from backend.repositories.artist_tag_files import ArtistTagFilesRepository


@dataclass(frozen=True)
class FakeTagFile:
    """One ``library_files`` row, as far as the read cares."""

    normalized_name: str | None
    tag_value: str | None
    artist_name: str = "Some Artist"
    track_title: str | None = "Some Song"
    present: bool = True


class FakeArtistTagFilesRepository(ArtistTagFilesRepository):
    def __init__(self) -> None:
        self.files: list[FakeTagFile] = []

    def list_tag_groups(self) -> list[ArtistTagGroup]:
        tagged = [
            f
            for f in self.files
            if f.present and f.normalized_name and f.tag_value and f.normalized_name.strip()
        ]
        values: dict[str, set[str]] = {}
        for file in tagged:
            assert file.normalized_name is not None and file.tag_value is not None
            values.setdefault(file.normalized_name, set()).add(file.tag_value.lower())
        groups = Counter(
            (f.normalized_name, f.tag_value, f.artist_name, f.track_title)
            for f in tagged
            if f.normalized_name is not None and len(values[f.normalized_name]) > 1
        )
        return [
            ArtistTagGroup(
                normalized_name=str(name),
                tag_value=str(value),
                artist_name=spelling,
                track_title=title,
                files=files,
            )
            for (name, value, spelling, title), files in groups.items()
        ]
