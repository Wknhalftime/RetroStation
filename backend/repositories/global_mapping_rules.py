from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import GlobalMappingRule


class GlobalMappingRuleRepository(ABC):
    @abstractmethod
    def list_ordered(self) -> list[GlobalMappingRule]:
        """Return all rules ORDER BY priority DESC. First match wins in callers."""
        ...

    @abstractmethod
    def create(self, rule: GlobalMappingRule) -> GlobalMappingRule: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...
