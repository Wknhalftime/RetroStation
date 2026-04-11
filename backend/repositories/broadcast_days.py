from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

from backend.domain.broadcast import BroadcastDay


class BroadcastDayRepository(ABC):
    @abstractmethod
    def get_or_create(self, station_id: UUID, broadcast_date: date) -> BroadcastDay: ...

    @abstractmethod
    def get_dates_for_station(self, station_id: UUID) -> list[date]: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> BroadcastDay | None: ...
