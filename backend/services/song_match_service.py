"""The one writer of song matches (artist model phase 3a PR 1; C52, D2, AUD-R040).

Every match decision and every write call on the matches port goes through
``SongMatchService``: the auto-matcher's result, Resolve's manual match, Reject's and
Unmatch's clearing, a missing file's release and fold, and the moves that carry a file's
matches to its work (``tests/test_song_match_single_writer.py``). It is sync and writes in
the caller's transaction: it commits nothing (C75). The repository cascades the SPEC names
stay in their repositories.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.enums import MatchTier, TargetType
from backend.domain.matching import Match
from backend.repositories.matches import MatchRepository


class SongMatchService:
    """Song matches, written through the matches port in the caller's transaction."""

    def __init__(self, matches: MatchRepository) -> None:
        self._matches = matches

    def record_result(self, identity_id: UUID, suggestion: Match | None) -> None:
        """The matcher's result replaces whatever the song showed: its suggestion, or none."""
        self._matches.delete_for_identity(identity_id)
        if suggestion is not None:
            self._matches.create(suggestion)

    def match_by_hand(self, identity_id: UUID, file_id: UUID, work_id: str | None) -> None:
        """The curator's pick replaces the song's matches (Resolve)."""
        self._matches.delete_for_identity(identity_id)
        self._matches.create(
            Match(
                id=uuid4(),
                identity_id=identity_id,
                library_file_id=file_id,
                target_type=TargetType.LIBRARY_FILE,
                work_id=work_id,
                confidence_score=1.0,
                match_tier=MatchTier.MANUAL,
            )
        )

    def clear_song(self, identity_id: UUID) -> None:
        """Delete the song's matches (Reject, Unmatch)."""
        self._matches.delete_for_identity(identity_id)

    def release_file(self, file_id: UUID) -> list[UUID]:
        """Delete every match naming the file; the identity id of each match deleted."""
        return self._matches.delete_for_file(file_id)

    def follow_file(self, file_id: UUID, work_id: str | None) -> None:
        """Point every match on the file at the work the file now belongs to."""
        self._matches.move_to_work(file_id, work_id)

    def follow_works(self, work_ids: list[str], target_id: str) -> None:
        """Point every match at *work_ids*, as its target or its file's work, at *target_id*."""
        self._matches.move_matches_of_works(work_ids, target_id)

    def release_work(self, work_id: str) -> None:
        """Point each match still on *work_id* at its file's current work (or none)."""
        self._matches.release_work_matches(work_id)

    def hand_off_performance(self, match_ids: list[UUID], performance_id: UUID) -> None:
        """The lines store *performance_id* instead (phase 3a, E3, D5)."""
        self._matches.set_stored_performance(match_ids, performance_id)

    def release_performance(self, match_ids: list[UUID]) -> None:
        """The lines store no performance: they follow the work's default (E3, D5)."""
        self._matches.set_stored_performance(match_ids, None)
