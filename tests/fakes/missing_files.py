from backend.domain.library import MissingFileListing, MissingFileRow
from backend.repositories.missing_files import MissingFileListingRepository


class FakeMissingFileListingRepository(MissingFileListingRepository):
    """Serves rows a test seeds; the SQL that derives them has integration tests."""

    def __init__(self) -> None:
        self._rows: list[MissingFileRow] = []

    def seed(self, rows: list[MissingFileRow]) -> None:
        self._rows = sorted(rows, key=lambda r: r.file_path)

    def list_page(self, offset: int, limit: int) -> MissingFileListing:
        return MissingFileListing(
            rows=tuple(self._rows[offset : offset + limit]),
            total=len(self._rows),
            total_match_count=sum(r.match_count for r in self._rows),
        )
