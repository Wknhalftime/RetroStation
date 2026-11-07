from abc import ABC, abstractmethod
from collections.abc import Collection

from backend.domain.catalog import Artist
from backend.domain.enums import ArtistNameSource


class ArtistCatalogRepository(ABC):
    @abstractmethod
    def upsert(self, artist: Artist) -> Artist: ...

    @abstractmethod
    def get_by_id(self, artist_id: str) -> Artist | None: ...

    @abstractmethod
    def get_by_mbid(self, mbid: str) -> Artist | None:
        """The artist holding this MBID, if any."""
        ...

    @abstractmethod
    def create_stub(self, mbid: str) -> Artist:
        """A stub artist for an MBID no artist holds (phase 1a, S3): a new id, the MBID as its
        name and sort name, no normalized name, origin musicbrainz, needing enhancement.
        Enrichment names it later. When another writer has meanwhile stored an artist with
        the MBID, that artist comes back instead."""
        ...

    @abstractmethod
    def list_all(self) -> list[Artist]:
        """Return all artists for fuzzy-matching in artist_matching_service, without the
        unnamed stubs (a name still equal to the MBID, phase 1a S15)."""
        ...

    @abstractmethod
    def upsert_local_artist(self, name: str, normalized_name: str) -> str:
        """The artist a file of this name groups under (phase 2a S7). None of the name: a new
        name-only artist. One, on a name holding at most one MBID: that artist, with or without
        an MBID. Several, or a name holding several MBIDs: the single name-only artist, created
        if absent, so the result doesn't depend on which file the scan reaches first.
        Returns artist id.
        """
        ...

    @abstractmethod
    def upsert_musicbrainz_artist(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
        *,
        name_source: ArtistNameSource = ArtistNameSource.MUSICBRAINZ_NAME,
    ) -> str:
        """The artist of this MBID (phase 2a S6): found by MBID, that artist. Otherwise, when the
        name's MBIDs are none or only this one (``name_mbids``, D5), the name-only artist of the
        name takes it in place; failing that, a new artist. It never returns another MBID's artist.

        The caller is responsible for computing ``normalized_name`` via
        ``backend.services.normalization.normalize_artist`` before calling
        this method; this keeps the repository layer free of service imports.

        ``name_source`` says where the name came from, for ``artist_names`` (S10): MusicBrainz, or
        a tag when the sweep names an artist.

        Returns artist id (may be a promoted local UUID).
        """
        ...

    @abstractmethod
    def upsert_further_credit(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
    ) -> str:
        """As ``upsert_musicbrainz_artist``, for a credit after the first (phase 2a S8, D5): the
        name-only artist takes the MBID only when the name's MBIDs are exactly this one. With none,
        a new artist goes beside it, because a further credit is new and no tag agrees."""
        ...

    @abstractmethod
    def artist_ids_by_mbid(self, mbids: Collection[str]) -> dict[str, str]:
        """The artist id of each of these MBIDs an artist holds, unnamed stubs included (phase 2a
        S10, S14): one read, where a caller would otherwise ask per MBID."""
        ...

    @abstractmethod
    def list_named_stubs_without_normalized_name(self) -> list[Artist]:
        """Stubs MusicBrainz has named that 1a left with no normalized name (phase 2a S13)."""
        ...

    @abstractmethod
    def set_normalized_name(self, artist_id: str, normalized_name: str) -> None:
        """Give one artist its normalized name, in the caller's transaction (phase 2a S13)."""
        ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> Artist | None:
        """The name-only artist of this normalized name, if any (phase 2a S5): names are unique
        only without an MBID (C41), so MBID artists of the name are not answered for here."""
        ...

    @abstractmethod
    def name_mbids(self, normalized_name: str) -> set[str]:
        """The MBIDs this normalized name holds (phase 2a S6, the count behind M33's 17): the
        lowercased, well-formed, non-placeholder values of present files whose ``artist_mbid``
        holds exactly one MBID."""
        ...
