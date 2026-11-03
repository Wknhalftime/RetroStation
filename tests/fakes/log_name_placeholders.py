"""In-memory fake of the not-an-artist list (artist model phase 1c, SPEC S6; C7)."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from backend.domain.enums import PlaceholderSource
from backend.repositories.log_name_placeholders import LogNamePlaceholderRepository


class FakeLogNamePlaceholderRepository(LogNamePlaceholderRepository):
    """The list as normalized name -> who added it. *listed* starts it, as migration 0045's
    seed starts the table."""

    def __init__(self, listed: Iterable[str] = ()) -> None:
        self.added_by: dict[str, PlaceholderSource] = {
            name: PlaceholderSource.MIGRATION for name in listed
        }

    def listed(self, normalized_names: Collection[str]) -> set[str]:
        return {name for name in normalized_names if name in self.added_by}

    def add(self, normalized_name: str, added_by: PlaceholderSource) -> bool:
        if normalized_name in self.added_by:
            return False
        self.added_by[normalized_name] = added_by
        return True

    def remove(self, normalized_name: str) -> bool:
        return self.added_by.pop(normalized_name, None) is not None
