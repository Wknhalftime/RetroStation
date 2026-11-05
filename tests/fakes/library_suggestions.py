"""In-memory fake of the library suggestions (artist model phase 2a, SPEC S13, S14; C21)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from backend.domain.enums import SuggestionReason, SuggestionState
from backend.domain.library import LibrarySuggestion
from backend.repositories.library_suggestions import LibrarySuggestionRepository


class FakeLibrarySuggestionRepository(LibrarySuggestionRepository):
    """The rows naming no file, by (normalized name, reason). ``set_state`` is what phase 5's page
    will do to a row, for the tests that a repeat leaves it alone."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, SuggestionReason], LibrarySuggestion] = {}

    def upsert_for_name(
        self,
        normalized_name: str,
        reason: SuggestionReason,
        candidates: Sequence[str],
        evidence: Mapping[str, object],
    ) -> bool:
        key = (normalized_name, reason)
        held = self.rows.get(key)
        row = LibrarySuggestion(
            normalized_name=normalized_name,
            reason=reason,
            candidates=tuple(candidates),
            evidence=dict(evidence),
            state=held.state if held is not None else SuggestionState.OPEN,
        )
        if row == held:
            return False
        self.rows[key] = row
        return True

    def get_for_name(
        self, normalized_name: str, reason: SuggestionReason
    ) -> LibrarySuggestion | None:
        return self.rows.get((normalized_name, reason))

    def set_state(
        self, normalized_name: str, reason: SuggestionReason, state: SuggestionState
    ) -> None:
        key = (normalized_name, reason)
        self.rows[key] = replace(self.rows[key], state=state)
