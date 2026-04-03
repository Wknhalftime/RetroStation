from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import TargetType
from backend.domain.models import GlobalMappingRule
from backend.repositories.global_mapping_rules import GlobalMappingRuleRepository


class PgGlobalMappingRuleRepository(GlobalMappingRuleRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> GlobalMappingRule:
        return GlobalMappingRule(
            id=row["id"],
            source_pattern=row["source_pattern"],
            target_type=TargetType(row["target_type"]),
            target_id=row["target_id"],
            priority=row["priority"],
            created_at=row["created_at"],
        )

    def list_ordered(self) -> list[GlobalMappingRule]:
        rows = self._conn.execute(
            "SELECT * FROM global_mapping_rules ORDER BY priority DESC"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def create(self, rule: GlobalMappingRule) -> GlobalMappingRule:
        self._conn.execute(
            """INSERT INTO global_mapping_rules
               (id, source_pattern, target_type, target_id, priority, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (rule.id, rule.source_pattern, rule.target_type.value,
             rule.target_id, rule.priority, rule.created_at),
        )
        row = self._conn.execute(
            "SELECT * FROM global_mapping_rules WHERE id = %s", (rule.id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def delete(self, id: UUID) -> None:
        self._conn.execute(
            "DELETE FROM global_mapping_rules WHERE id = %s", (id,)
        )
