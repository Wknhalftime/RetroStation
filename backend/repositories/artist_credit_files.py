"""A file's every artist (phase 2a S1, S3, S14): what the backfill and the sweep read and write.

Its own port, because the library-files fake is locked.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID

from backend.domain.library import ArtistCreditFile, CreditEntry, CreditGroup


class ArtistCreditFilesRepository(ABC):
    @abstractmethod
    def list_to_fill(self, after_id: UUID | None, limit: int) -> list[ArtistCreditFile]:
        """Rows with ``raw_metadata``, whatever their status, in id order after *after_id*, whose
        columns are unfilled or whose credit still has a null name. A row a scan wrote can be
        named here (S4). A row whose names are all there is never read again."""
        ...

    @abstractmethod
    def write_credit(
        self, file_id: UUID, artist_mbids: Sequence[str], credit: Sequence[CreditEntry] | None
    ) -> None:
        """Write one row's two columns, and nothing else (S3)."""
        ...

    @abstractmethod
    def list_credit_groups(self) -> list[CreditGroup]:
        """One group per (MBID, name) of the present files holding several real MBIDs, with its
        file count. A file holding one is left out, so tags alone never name an MBID (D10)."""
        ...

    @abstractmethod
    def list_tag_spellings(self) -> set[tuple[str, str]]:
        """Every (MBID, name) pair a credit holds, on any file (S10)."""
        ...
