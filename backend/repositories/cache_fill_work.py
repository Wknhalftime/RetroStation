"""The cache fill's work lists (phase 4.1 PR 2; SPEC S3; AUD-R091): the port.

Each method reads one tier's rows, most plays (or files) first; which of them still needs a call
is the fill's to decide, against the cache. Special-purpose MBIDs (C7) are never an artist here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection

from backend.domain.cache_fill import ManualTitle, NamePair, QueuedPair, UntaggedFile
from backend.domain.enums import MatchStatus


class CacheFillWorkRepository(ABC):
    @abstractmethod
    def searched_pairs(self, statuses: Collection[MatchStatus]) -> list[NamePair]:
        """The (name, normalized title) pairs of the log names in *statuses* that are not on the
        not-an-artist list, by plays, each with whether a present file holds its title and
        whether the title is common (C8: 3 or more distinct acts, the library's files by
        credited MBID or an untagged file's normalized artist name, and the log names that
        played it through their active links, by catalog artist)."""
        ...

    @abstractmethod
    def untagged_files(self) -> list[UntaggedFile]:
        """Present files with no artist MBID whose normalized artist text is no known act with
        a real MBID (an artist's normalized name or one of its ``artist_names``), grouped by
        artist text and title, most files first. Whether the text has a joiner is the
        caller's test."""
        ...

    @abstractmethod
    def manual_titles(self) -> list[ManualTitle]:
        """The titles of log names whose one active link is manual, to an artist holding a real
        MBID, by plays; never a not-an-artist name. Whether the name has a joiner is the
        caller's test."""
        ...

    @abstractmethod
    def confirmations(self) -> list[QueuedPair]:
        """Queued (name, title) pairs, oldest first, except for manually rejected names,
        not-an-artist names, and names whose active links are all manual (C18's)."""
        ...

    @abstractmethod
    def catalog_mbids(self) -> list[str]:
        """Every real MBID a catalog artist holds."""
        ...
