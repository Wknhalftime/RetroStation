from abc import ABC, abstractmethod


class RecordingSearchReachRepository(ABC):
    """Whether song matching's MusicBrainz recording search (Tier 1 Step B) could add a file."""

    @abstractmethod
    def has_file_beyond_artist_mbid(self, normalized_artist_name: str, artist_mbid: str) -> bool:
        """True when a PRESENT file with this ``normalized_artist_name`` carries a
        recording_mbid and is tagged with *artist_mbid* neither as its artist_mbid nor as its
        album_artist_mbid.

        Step B keeps only present files with the artist's normalized name, found by
        recording_mbid; a file tagged with the artist MBID is one Step A already scored. So
        when this is False, Step B cannot change the song's result.
        """
        ...
