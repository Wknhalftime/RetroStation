from abc import ABC, abstractmethod

from backend.domain.library import MissingFileListing


class MissingFileListingRepository(ABC):
    """Read model for the Missing Files page."""

    @abstractmethod
    def list_page(self, offset: int, limit: int) -> MissingFileListing:
        """MISSING rows in file_path order from *offset*, at most *limit*, plus totals."""
        ...
