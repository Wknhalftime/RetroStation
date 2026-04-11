# LogLevel and LogCategory enums (in enums.py) belong to this subdomain.
# They are application-logging concerns, not broadcast-log models.
# The Log* prefix here refers to observability — not playlist ingestion.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType


@dataclass(frozen=True)
class MusicBrainzCache:
    id: UUID
    cache_key: str
    entity_type: str
    entity_mbid: str
    response_data: dict[str, Any]
    cached_at: datetime
    expires_at: datetime


@dataclass
class TaskProgress:
    task_id: str
    task_type: TaskType
    status: TaskStatus
    progress_data: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


@dataclass
class UserSetting:
    key: str
    value: str
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SystemLog:
    category: LogCategory
    level: LogLevel
    message: str
    id: UUID = field(default_factory=uuid4)
    trace_id: str | None = None
    details: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
