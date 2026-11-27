"""In-memory LinkEvidenceRepository (artist model phase 4.2 PR 1, SPEC S2; design C8, C32, C43,
C67, M33).

What the linker's local sources read: a name's played titles, most played first; the credits of
the library's present files holding a title; the performers an exact name points at (an
artist's own name or one of its ``artist_names``, through Merge to the survivor, M33); the
catalog, for the close-name shortlist; C8's act count per title; and whether a name holds a
manual link.

C8 counts distinct acts: each performer of a file's credit once, and each act a log name playing
the title is actively linked to once, so two log spellings of one act count once; a merged
artist counts as the artist it merged into (M33). A played title the log cut short ("...") is
asked as a prefix, and matches every library title it starts (C67).
"""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from backend.domain.linking import Credit, Performer, PlayedTitle, TitleActs
from backend.repositories.link_evidence import LinkEvidenceRepository
from backend.services.normalization import normalize_artist


class FakeLinkEvidenceRepository(LinkEvidenceRepository):
    def __init__(self) -> None:
        self._names: dict[str, set[str]] = {}  # performer key -> its normalized names
        self._performers: dict[str, Performer] = {}
        self._merged_into: dict[str, str] = {}
        self._files: list[tuple[str, Credit]] = []  # (normalized title, credit)
        self._plays: dict[UUID, dict[str, PlayedTitle]] = {}
        self._links: dict[UUID, set[str]] = {}  # active log-side links, by performer key
        self._manual: set[UUID] = set()

    # --- building the world ---------------------------------------------------------------------

    def add_artist(
        self, key: str, name: str, *, also: Collection[str] = (), merged_into: str | None = None
    ) -> Performer:
        performer = Performer(key, name)
        self._performers[key] = performer
        self._names[key] = {normalize_artist(n) for n in (name, *also)}
        if merged_into is not None:
            self._merged_into[key] = merged_into
        return performer

    def add_file(self, title: str, *performers: Performer) -> None:
        """A present library file of the normalized *title*, credited to *performers*."""
        self._files.append((title, Credit(tuple(performers))))

    def play(self, name_id: UUID, title: str, plays: int, *, original: str | None = None) -> None:
        self._plays.setdefault(name_id, {})[title] = PlayedTitle(title, original or title, plays)

    def link_active(self, name_id: UUID, performer: Performer) -> None:
        self._links.setdefault(name_id, set()).add(performer.key)

    def link_manual(self, name_id: UUID) -> None:
        self._manual.add(name_id)

    # --- the port --------------------------------------------------------------------------------

    def played_titles(self, broadcast_artist_id: UUID) -> list[PlayedTitle]:
        played = self._plays.get(broadcast_artist_id, {}).values()
        return sorted(played, key=lambda p: (-p.plays, p.normalized_title))

    def _matches(self, wanted: str, title: str, prefixes: Collection[str]) -> bool:
        """A played title cut short ("..." in the log, C67) is passed as a prefix: it matches
        every library title it starts."""
        return title.startswith(wanted) if wanted in prefixes else title == wanted

    def library_credits(
        self, normalized_titles: Collection[str], prefixes: Collection[str] = ()
    ) -> dict[str, list[Credit]]:
        found: dict[str, list[Credit]] = {}
        for wanted in (*normalized_titles, *prefixes):
            for title, filed in self._files:
                credit = self._survivors(filed)
                if self._matches(wanted, title, prefixes) and credit not in found.get(wanted, []):
                    found.setdefault(wanted, []).append(credit)
        return found

    def _survivors(self, credit: Credit) -> Credit:
        """A file credited to a merged artist is its survivor's (M33)."""
        return Credit(
            tuple(self._performers[self._merged_into.get(p.key, p.key)] for p in credit.performers)
        )

    def performers_named(self, normalized_name: str) -> list[Performer]:
        keys = {
            self._merged_into.get(key, key)
            for key, names in self._names.items()
            if normalized_name in names
        }
        return [self._performers[key] for key in sorted(keys)]

    def catalog(self) -> list[Performer]:
        return [p for key, p in sorted(self._performers.items()) if key not in self._merged_into]

    def title_acts(
        self, normalized_titles: Collection[str], prefixes: Collection[str] = ()
    ) -> dict[str, TitleActs]:
        """C8: library artists grouped by MBID, so a merged artist counts as its survivor."""
        counts: dict[str, TitleActs] = {}
        for wanted in (*normalized_titles, *prefixes):
            acts = {
                p.key
                for t, credit in self._files
                if self._matches(wanted, t, prefixes)
                for p in credit.performers
            }
            for name_id, played in self._plays.items():
                if wanted in played:
                    acts |= self._links.get(name_id, set())
            acts = {self._merged_into.get(key, key) for key in acts}
            sole = next(iter(acts)) if len(acts) == 1 else None
            counts[wanted] = TitleActs(count=len(acts), sole=sole)
        return counts

    def holds_manual_link(self, broadcast_artist_id: UUID) -> bool:
        return broadcast_artist_id in self._manual
