from collections.abc import Sequence
from datetime import date
from uuid import UUID

from backend.domain.streaming import ScheduleItem
from backend.repositories.playable_schedule import PlayableScheduleRepository


class FakePlayableScheduleRepository(PlayableScheduleRepository):
    """In-memory schedule: each station-day is exactly what a test set with ``set_day``."""

    def __init__(self) -> None:
        self._days: dict[tuple[UUID, date], list[ScheduleItem]] = {}

    def set_day(self, station_id: UUID, day: date, items: Sequence[ScheduleItem]) -> None:
        """Set a day; like the real reader's, its items must be in logged order."""
        for earlier, later in zip(items, items[1:], strict=False):
            if later.logged_at < earlier.logged_at:
                raise ValueError(
                    f"set_day items must be in logged order: {later.logged_at} "
                    f"follows {earlier.logged_at}"
                )
        self._days[(station_id, day)] = list(items)

    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        return list(self._days.get((station_id, day), []))
