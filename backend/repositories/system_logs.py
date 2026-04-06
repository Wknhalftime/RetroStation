from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.enums import LogCategory, LogLevel
from backend.domain.models import SystemLog


class SystemLogRepository(ABC):
    @abstractmethod
    def create(self, log: SystemLog) -> None: ...

    @abstractmethod
    def list(
        self,
        *,
        level: LogLevel | None = None,
        category: LogCategory | None = None,
        trace_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SystemLog]: ...

    @abstractmethod
    def count(
        self,
        *,
        level: LogLevel | None = None,
        category: LogCategory | None = None,
        trace_id: str | None = None,
    ) -> int: ...

