from backend.domain.enums import SelectionMethod
from backend.domain.curation import SongMaster
from backend.repositories.song_masters import SongMasterRepository


class FakeSongMasterRepository(SongMasterRepository):
    def __init__(self) -> None:
        self._data: dict[str, SongMaster] = {}  # keyed by work_id

    def upsert(self, master: SongMaster) -> SongMaster:
        self._data[master.work_id] = master
        return master

    def get_by_work(self, work_id: str) -> SongMaster | None:
        return self._data.get(work_id)

    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        return [
            m for work_id, m in self._data.items()
            if work_id in work_ids and m.selection_method == SelectionMethod.AUTO
        ]
