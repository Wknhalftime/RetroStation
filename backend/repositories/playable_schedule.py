from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

from backend.domain.streaming import ScheduleItem


class PlayableScheduleRepository(ABC):
    """Reads a station's logged days as playable schedules (spec: Data)."""

    @abstractmethod
    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        """Every play the station logged on ``day``, in station-day position order (D19).

        Position orders by ``(played_at, identity_id, event_id)``, so item ``i`` is the play
        at position ``i``. Each play carries the file it resolves to, or ``None``; an empty
        list when the station logged nothing that day.
        """
        ...
