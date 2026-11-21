"""What the artist list and an artist's page read about works (artist model phase 3b PR 5b;
SPEC S7, scope note R6, R9): a work's artists are its performances' credits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ListedWorks:
    """A listed artist's works, each work under the root of each credited artist's merge group,
    and how many files sit on them."""

    work_ids: tuple[str, ...]
    file_count: int


@dataclass(frozen=True)
class GroupWork:
    """A work some member of an artist's merge group is credited on, and the members credited."""

    id: str
    title: str
    mbid: str | None
    origin: str
    recording_count: int
    has_master: bool
    version_types: tuple[str, ...]
    credited: frozenset[str]
