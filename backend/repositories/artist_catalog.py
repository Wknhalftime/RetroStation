from abc import ABC, abstractmethod

from backend.domain.catalog import Artist


class ArtistCatalogRepository(ABC):
    @abstractmethod
    def upsert(self, artist: Artist) -> Artist: ...

    @abstractmethod
    def get_by_id(self, artist_id: str) -> Artist | None: ...

    @abstractmethod
    def list_all(self) -> list[Artist]:
        """Return all artists for fuzzy-matching in artist_matching_service."""
        ...

    @abstractmethod
    def upsert_local_artist(self, name: str, normalized_name: str) -> str:
        """Create local artist or return existing by normalized_name.
        INSERT ON CONFLICT (normalized_name) DO NOTHING + retry-SELECT.
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
    ) -> str:
        """Lookup by mbid or normalized_name, promote/create/reuse.

        The caller is responsible for computing ``normalized_name`` via
        ``backend.services.normalization.normalize_artist`` before calling
        this method; this keeps the repository layer free of service imports.

        Returns artist id (may be a promoted local UUID).
        """
        ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> Artist | None: ...
