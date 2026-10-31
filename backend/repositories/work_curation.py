"""The works writes' own statements: what ``work_curation_service`` needs beyond the
works, library file, recording, song master, format override, match and artist repositories.

Each method runs one or two of the statements the works router ran before phase 0c (C75),
unchanged.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.catalog import Recording, RecordingVersion
from backend.domain.curation import FormatOverride, MasterCandidate


class WorkCurationRepository(ABC):
    # --- merge ---------------------------------------------------------------------------

    @abstractmethod
    def move_files_of_works(self, work_ids: list[str], target_id: str) -> int:
        """Put every file of *work_ids* on *target_id*; how many moved (0 when unknown)."""
        ...

    @abstractmethod
    def drop_colliding_overrides(self, work_ids: list[str], target_id: str) -> int:
        """Delete each override of *work_ids* whose format the target has (the target's
        wins), or that another of them holds with an earlier (created_at, id); how many."""
        ...

    @abstractmethod
    def move_overrides_of_works(self, work_ids: list[str], target_id: str) -> None:
        """Put every override of *work_ids* on *target_id*."""
        ...

    @abstractmethod
    def recordings_of_works(self, work_ids: list[str]) -> list[RecordingVersion]:
        """Every recording of *work_ids*, in no particular order."""
        ...

    @abstractmethod
    def move_recording_files(self, from_recording_id: str, to_recording_id: str) -> None:
        """Put every file of one recording on another."""
        ...

    @abstractmethod
    def delete_recording(self, recording_id: str) -> None:
        """Delete a recording."""
        ...

    @abstractmethod
    def move_recording(self, recording_id: str, work_id: str) -> None:
        """Put a recording on *work_id*."""
        ...

    @abstractmethod
    def move_matches_of_works(self, work_ids: list[str], target_id: str) -> None:
        """Point every match at *work_ids*, as its target or its file's work, at *target_id*."""
        ...

    @abstractmethod
    def delete_masters_of_works(self, work_ids: list[str]) -> None:
        """Delete the song masters of *work_ids*, whatever their method."""
        ...

    @abstractmethod
    def delete_works(self, work_ids: list[str]) -> int:
        """Delete the works; how many existed."""
        ...

    @abstractmethod
    def artist_has_match(self, artist_id: str) -> bool:
        """Whether an artist match targets the catalog artist (``target_id`` has no FK)."""
        ...

    @abstractmethod
    def delete_artist(self, artist_id: str) -> None:
        """Delete a catalog artist."""
        ...

    # --- split and reassign --------------------------------------------------------------

    @abstractmethod
    def insert_recording(self, recording: Recording) -> None:
        """Store a new recording, as not needing MusicBrainz enhancement."""
        ...

    @abstractmethod
    def set_file_recording(self, file_id: UUID, recording_id: str) -> None:
        """Put a file on a recording."""
        ...

    @abstractmethod
    def count_other_files_on_recording(self, recording_id: str, file_id: UUID) -> int:
        """How many files besides *file_id* the recording holds."""
        ...

    @abstractmethod
    def store_split_master(self, master_id: UUID, work_id: str, file_id: UUID) -> None:
        """Store the new work's automatic master on the split-off file, with no score."""
        ...

    # --- a work a file left ----------------------------------------------------------------

    @abstractmethod
    def release_work_matches(self, work_id: str) -> None:
        """Point each match still on *work_id* at its file's current work (or none)."""
        ...

    @abstractmethod
    def delete_overrides_of_work(self, work_id: str) -> None:
        """Delete every override of the work."""
        ...

    @abstractmethod
    def delete_recordings_of_work(self, work_id: str) -> None:
        """Delete every recording of the work."""
        ...

    # --- the master recalculation ------------------------------------------------------------

    @abstractmethod
    def present_master_candidates(self, work_id: str) -> list[MasterCandidate]:
        """The work's present files, in no particular order."""
        ...

    @abstractmethod
    def delete_master_off_work(self, work_id: str) -> None:
        """Delete the work's master if its file is not one of the work's files."""
        ...

    @abstractmethod
    def store_auto_master(self, master_id: UUID, work_id: str, file_id: UUID, score: int) -> None:
        """Store the work's automatic master, replacing whatever master it has."""
        ...

    # --- format overrides ------------------------------------------------------------------

    @abstractmethod
    def insert_override_if_absent(self, override: FormatOverride) -> bool:
        """Store the override unless the work has one for its format; whether it was stored."""
        ...
