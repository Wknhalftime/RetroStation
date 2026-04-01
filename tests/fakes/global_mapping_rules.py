from uuid import UUID

from backend.domain.models import GlobalMappingRule
from backend.repositories.global_mapping_rules import GlobalMappingRuleRepository


class FakeGlobalMappingRuleRepository(GlobalMappingRuleRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, GlobalMappingRule] = {}

    def list_ordered(self) -> list[GlobalMappingRule]:
        return sorted(self._data.values(), key=lambda r: r.priority, reverse=True)

    def create(self, rule: GlobalMappingRule) -> GlobalMappingRule:
        self._data[rule.id] = rule
        return rule

    def delete(self, id: UUID) -> None:
        self._data.pop(id, None)
