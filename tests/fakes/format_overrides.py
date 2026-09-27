from dataclasses import replace
from uuid import UUID

from backend.domain.curation import FormatOverride
from backend.repositories.format_overrides import FormatOverrideRepository


class FakeFormatOverrideRepository(FormatOverrideRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, FormatOverride] = {}

    def create(self, override: FormatOverride) -> FormatOverride:
        self._data[override.id] = override
        return override

    def get(self, work_id: str, format_name: str) -> FormatOverride | None:
        return next(
            (
                o
                for o in self._data.values()
                if o.work_id == work_id and o.format_name == format_name
            ),
            None,
        )

    def list_by_work(self, work_id: str) -> list[FormatOverride]:
        return [o for o in self._data.values() if o.work_id == work_id]

    def delete(self, override_id: UUID) -> None:
        self._data.pop(override_id, None)

    def move_to_work(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        to_work_formats = {o.format_name for o in self._data.values() if o.work_id == to_work_id}
        for override_id, override in list(self._data.items()):
            if override.work_id != from_work_id or override.preferred_file_id != file_id:
                continue
            if override.format_name in to_work_formats:
                del self._data[override_id]
            else:
                self._data[override_id] = replace(override, work_id=to_work_id)
