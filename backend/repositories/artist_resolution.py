"""The Resolution Center's artist Resolve and Unmatch: what they write on a log name and its
songs, moved from ``routers/matching.py`` unchanged when the two routes became sync routes
(phase 1a, C75). A log name's links are written through ``LogNameLinkService``, never here.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import MatchStatus


class ArtistResolutionRepository(ABC):
    @abstractmethod
    def exists(self, broadcast_artist_id: UUID) -> bool:
        """Whether the log name exists."""
        ...

    @abstractmethod
    def status_of(self, broadcast_artist_id: UUID) -> MatchStatus | None:
        """The log name's status; None when it does not exist."""
        ...

    @abstractmethod
    def set_status(self, broadcast_artist_id: UUID, status: MatchStatus) -> None:
        """Set the log name's status, and nothing else (its reason stays)."""
        ...

    @abstractmethod
    def reopen_songs_in_review(self, broadcast_artist_id: UUID) -> None:
        """Put the name's pending and needs_review songs back to pending, tier and reason
        cleared, so the next run re-matches them against the name's new link."""
        ...

    @abstractmethod
    def delete_pending_song_matches(self, broadcast_artist_id: UUID) -> None:
        """Delete the match rows of every pending song of the name."""
        ...

    @abstractmethod
    def playlists_playing(self, broadcast_artist_id: UUID) -> list[UUID]:
        """Every playlist that played one of the name's songs."""
        ...

    @abstractmethod
    def reject_open_songs(self, broadcast_artist_id: UUID) -> None:
        """Set every song of the name the curator has not decided to auto_rejected."""
        ...

    @abstractmethod
    def unmatch_if_decided(self, broadcast_artist_id: UUID) -> bool:
        """Put a matched or rejected log name back in review (USER_UNMATCHED), in one
        statement; whether it was in such a state."""
        ...

    @abstractmethod
    def reopen_every_song(self, broadcast_artist_id: UUID) -> None:
        """Put every song of the name back in review (USER_UNMATCHED), whatever its status."""
        ...

    @abstractmethod
    def delete_song_matches(self, broadcast_artist_id: UUID) -> None:
        """Delete the match rows of every song of the name."""
        ...
