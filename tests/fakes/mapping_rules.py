from uuid import UUID

from backend.domain.matching import MappingRule
from backend.repositories.mapping_rules import MappingRuleRepository


class FakeMappingRuleRepository(MappingRuleRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, MappingRule] = {}

    def list_ordered(self) -> list[MappingRule]:
        return sorted(self._data.values(), key=lambda r: r.priority, reverse=True)

    def create(self, rule: MappingRule) -> MappingRule:
        self._data[rule.id] = rule
        return rule

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
