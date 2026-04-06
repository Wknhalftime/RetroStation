from __future__ import annotations

from backend.domain.enums import LogCategory, LogLevel
from backend.domain.models import SystemLog
from backend.repositories.system_logs import SystemLogRepository


class FakeSystemLogRepository(SystemLogRepository):
    def __init__(self) -> None:
        self._data: list[SystemLog] = []

    @property
    def all(self) -> list[SystemLog]:
        return list(self._data)

    def create(self, log: SystemLog) -> None:
        self._data.append(log)

    def list(
        self,
        *,
        level: LogLevel | None = None,
        category: LogCategory | None = None,
        trace_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SystemLog]:
        results = self._data
        if level is not None:
            results = [r for r in results if r.level == level]
        if category is not None:
            results = [r for r in results if r.category == category]
        if trace_id is not None:
            results = [r for r in results if r.trace_id == trace_id]
        results = sorted(results, key=lambda r: r.created_at, reverse=True)
        return results[offset : offset + limit]

    def count(
        self,
        *,
        level: LogLevel | None = None,
        category: LogCategory | None = None,
        trace_id: str | None = None,
    ) -> int:
        results = self._data
        if level is not None:
            results = [r for r in results if r.level == level]
        if category is not None:
            results = [r for r in results if r.category == category]
        if trace_id is not None:
            results = [r for r in results if r.trace_id == trace_id]
        return len(results)

