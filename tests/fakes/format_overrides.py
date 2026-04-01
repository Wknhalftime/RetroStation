from uuid import UUID
from backend.domain.models import FormatOverride
from backend.repositories.format_overrides import FormatOverrideRepository


class FakeFormatOverrideRepository(FormatOverrideRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, FormatOverride] = {}

    def create(self, override: FormatOverride) -> FormatOverride:
        self._data[override.id] = override
        return override

    def get(self, work_id: str, format_name: str) -> FormatOverride | None:
        return next(
            (o for o in self._data.values()
             if o.work_id == work_id and o.format_name == format_name), None
        )

    def list_by_work(self, work_id: str) -> list[FormatOverride]:
        return [o for o in self._data.values() if o.work_id == work_id]

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
