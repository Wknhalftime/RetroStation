from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.matching import MappingRule


class MappingRuleRepository(ABC):
    @abstractmethod
    def list_ordered(self) -> list[MappingRule]:
        """Return all rules ORDER BY priority DESC. First match wins in callers."""
        ...

    @abstractmethod
    def create(self, rule: MappingRule) -> MappingRule: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...
