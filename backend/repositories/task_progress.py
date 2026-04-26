from abc import ABC, abstractmethod
from typing import Any

from backend.domain.system import TaskProgress


class TaskProgressRepository(ABC):
    @abstractmethod
    def upsert(self, task: TaskProgress) -> TaskProgress: ...

    @abstractmethod
    def get_by_id(self, task_id: str) -> TaskProgress | None: ...

    @abstractmethod
    def list_running(self) -> list[TaskProgress]: ...

    @abstractmethod
    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        """Mark running tasks not updated in N minutes as FAILED. Returns count updated."""
        ...

    @abstractmethod
    def touch_running(self, task_id: str, progress_overlay: dict[str, Any]) -> int:
        """Refresh ``updated_at`` and shallow-merge ``progress_overlay`` into
        ``progress_data`` for an existing row, **forcing** ``status='running'``
        and clearing ``completed_at`` — including resurrection of rows that
        the WS stale-cleanup at ``backend/websocket.py`` tentatively flipped
        to ``failed``.

        The unconditional status overwrite IS the contract, not an
        unintended side effect. Implementations MUST NOT add an
        ``AND status = 'running'`` (or any "not in terminal states")
        guard to the UPDATE — that would silently break the resurrection
        path that this method exists to provide.

        Returns the affected ``rowcount``. Does NOT swallow DB errors —
        the layer above (heartbeat emitter) chooses the error policy.

        Used by the mb_enrichment heartbeat path to keep the WS broadcast
        alive during long pre-pass phases. Heartbeats overlay only
        ``current_item`` / ``phase`` / ``prepass_current`` / ``prepass_total``;
        other keys must use ``upsert`` (full document replace).
        """
        ...

