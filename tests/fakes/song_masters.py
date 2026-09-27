from backend.domain.curation import SongMaster
from backend.domain.enums import FileStatus, SelectionMethod
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.song_masters import SongMasterRepository


class FakeSongMasterRepository(SongMasterRepository):
    def __init__(self) -> None:
        self._data: dict[str, SongMaster] = {}  # keyed by work_id
        # File statuses are the library file repo's; tests wire one in via
        # set_library_file_repo for the missing-master listing.
        self._library_file_repo: LibraryFileRepository | None = None

    def upsert(self, master: SongMaster) -> SongMaster:
        self._data[master.work_id] = master
        return master

    def get_by_work(self, work_id: str) -> SongMaster | None:
        return self._data.get(work_id)

    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        return [
            m
            for work_id, m in self._data.items()
            if work_id in work_ids and m.selection_method == SelectionMethod.AUTO
        ]

    def list_work_ids_with_missing_master(self) -> list[str]:
        # Mirrors PgSongMasterRepository: the master's file is MISSING and the
        # work has a PRESENT file. Without a library file repo nothing is known.
        files = self._library_file_repo
        if files is None:
            return []
        stranded = []
        for work_id, master in self._data.items():
            chosen = files.get_by_id(master.preferred_file_id)
            if chosen is None or chosen.file_status != FileStatus.MISSING:
                continue
            if any(f.file_status == FileStatus.PRESENT for f in files.get_by_work(work_id)):
                stranded.append(work_id)
        return sorted(stranded)

    def set_library_file_repo(self, repo: LibraryFileRepository) -> None:
        """Inject the library file repo the missing-master listing reads statuses from."""
        self._library_file_repo = repo
