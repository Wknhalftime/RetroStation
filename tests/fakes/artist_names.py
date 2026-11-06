"""In-memory fake of an artist's names (artist model phase 2a, SPEC S10; C43)."""

from __future__ import annotations

from backend.domain.catalog import ArtistName
from backend.domain.enums import ArtistNameSource
from backend.repositories.artist_names import ArtistNameRepository


class FakeArtistNameRepository(ArtistNameRepository):
    def __init__(self) -> None:
        self.rows: list[tuple[str, ArtistName]] = []

    def add(
        self, artist_id: str, name: str, normalized_name: str, source: ArtistNameSource
    ) -> bool:
        row = (artist_id, ArtistName(name, normalized_name, source))
        if row in self.rows:
            return False
        self.rows.append(row)
        return True

    def list_for_artist(self, artist_id: str) -> list[ArtistName]:
        return sorted(
            (name for held, name in self.rows if held == artist_id),
            key=lambda name: (name.source.value, name.name),
        )
