# LogLevel and LogCategory enums (in enums.py) belong to this subdomain.
# They are application-logging concerns, not broadcast-log models.
# The Log* prefix here refers to observability — not playlist ingestion.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType


class SystemDomainError(Exception):
    """Base class for system-subdomain failures (settings, caches, logs, task progress).

    Not ``SystemError``: that name is a Python builtin.
    """


class StorageUnavailableError(SystemDomainError):
    """The database connection was lost mid-operation; a later attempt may succeed."""


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


class SettingsError(SystemDomainError):
    """Base class for user-settings refusals (D27; PR G1). Not a ``StorageUnavailableError``:
    a refused value answers 422, a lost connection 503."""


class InvalidSettingError(SettingsError):
    """A setting's value fails the rule for its key (D27): refused, naming the key."""


class ManagedSettingError(SettingsError):
    """A setting is written only by its own path; the generic save refuses it (PG2)."""


@dataclass
class SystemLog:
    category: LogCategory
    level: LogLevel
    message: str
    id: UUID = field(default_factory=uuid4)
    trace_id: str | None = None
    details: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
