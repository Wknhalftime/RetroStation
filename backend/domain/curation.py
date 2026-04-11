from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from backend.domain.enums import SelectionMethod


@dataclass
class SongMaster:
    id: UUID
    work_id: str
    preferred_file_id: UUID
    selection_method: SelectionMethod = SelectionMethod.AUTO
    score: int | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class FormatOverride:
    id: UUID
    work_id: str
    format_name: str
    preferred_file_id: UUID
    notes: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
