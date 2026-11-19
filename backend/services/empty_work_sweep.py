"""The empty-work sweep (work merges phase 3b PR 4; SPEC S6, D7; design C53, §6.9; AUD-R045,
AUD-R080): every work with no files goes through the one deletion rule, ``delete_if_empty``."""

from __future__ import annotations

from dataclasses import dataclass

from backend.repositories.works import WorkRepository


@dataclass(frozen=True)
class SweepResult:
    deleted: int
    kept: int


def sweep_empty_works(works: WorkRepository) -> SweepResult:
    """Delete each work with no files that nothing points at; the rest stay (listed for Lance
    by ``kept_empty_works``)."""
    candidates = works.list_empty_work_ids()
    deleted = sum(1 for work_id in candidates if works.delete_if_empty(work_id))
    return SweepResult(deleted=deleted, kept=len(candidates) - deleted)
