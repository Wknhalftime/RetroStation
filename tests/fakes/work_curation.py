"""In-memory :class:`WorkCurationRepository` over the other fakes' rows.

The works writes' statements reach across files, recordings, overrides, matches, masters and
artists, so this fake works on those fakes' own rows: what a statement changes, the other
fakes then read. ``tests/repositories/test_work_curation_contract.py`` runs the same cases
against it and against ``PgWorkCurationRepository``.

It enforces no foreign key or unique constraint. One the service can meet: emptying a work
whose recording a file on another work still holds fails in PostgreSQL (as the router did
before phase 0c), while this fake leaves that file's ``recording_id`` dangling.
"""

from dataclasses import replace
from uuid import UUID

from backend.domain.catalog import Recording, RecordingVersion
from backend.domain.curation import FormatOverride, MasterCandidate, SongMaster
from backend.domain.enums import FileStatus, SelectionMethod, TargetType
from backend.repositories.work_curation import WorkCurationRepository
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


class FakeWorkCurationRepository(WorkCurationRepository):
    def __init__(
        self,
        *,
        works: FakeWorkRepository,
        files: FakeLibraryFileRepository,
        recordings: FakeRecordingRepository,
        song_masters: FakeSongMasterRepository,
        overrides: FakeFormatOverrideRepository,
        matches: FakeMatchRepository,
        artists: FakeArtistRepository,
    ) -> None:
        self._works = works._data
        self._files = files._data
        self._recordings = recordings._data
        self._masters = song_masters._data
        self._overrides = overrides._data
        self._matches = matches._data
        self._artists = artists._data

    # --- merge ---------------------------------------------------------------------------

    def move_files_of_works(self, work_ids: list[str], target_id: str) -> int:
        moved = [f for f in self._files.values() if f.work_id in work_ids]
        for file in moved:
            self._files[file.id] = replace(file, work_id=target_id)
        return len(moved)

    def drop_colliding_overrides(self, work_ids: list[str], target_id: str) -> int:
        targets = {o.format_name for o in self._overrides.values() if o.work_id == target_id}
        sources = sorted(
            (o for o in self._overrides.values() if o.work_id in work_ids),
            key=lambda o: (o.created_at, o.id),
        )
        kept: dict[str, UUID] = {}
        for override in sources:
            if override.format_name not in targets:
                kept.setdefault(override.format_name, override.id)
        dropped = [o.id for o in sources if kept.get(o.format_name) != o.id]
        for override_id in dropped:
            del self._overrides[override_id]
        return len(dropped)

    def move_overrides_of_works(self, work_ids: list[str], target_id: str) -> None:
        for override in [o for o in self._overrides.values() if o.work_id in work_ids]:
            self._overrides[override.id] = replace(override, work_id=target_id)

    def recordings_of_works(self, work_ids: list[str]) -> list[RecordingVersion]:
        return [
            RecordingVersion(id=r.id, version_type=r.version_type)
            for r in self._recordings.values()
            if r.work_id in work_ids
        ]

    def move_recording_files(self, from_recording_id: str, to_recording_id: str) -> None:
        for file in [f for f in self._files.values() if f.recording_id == from_recording_id]:
            self._files[file.id] = replace(file, recording_id=to_recording_id)

    def delete_recording(self, recording_id: str) -> None:
        self._recordings.pop(recording_id, None)

    def move_recording(self, recording_id: str, work_id: str) -> None:
        recording = self._recordings.get(recording_id)
        if recording is not None:
            self._recordings[recording_id] = replace(recording, work_id=work_id)

    def move_matches_of_works(self, work_ids: list[str], target_id: str) -> None:
        for match in list(self._matches.values()):
            if match.target_type == TargetType.WORK and match.target_id in work_ids:
                self._matches[match.id] = match = replace(match, target_id=target_id)
            if match.work_id in work_ids:
                self._matches[match.id] = replace(match, work_id=target_id)

    def delete_masters_of_works(self, work_ids: list[str]) -> None:
        for work_id in work_ids:
            self._masters.pop(work_id, None)

    def delete_works(self, work_ids: list[str]) -> int:
        deleted = [w for w in dict.fromkeys(work_ids) if w in self._works]
        for work_id in deleted:
            del self._works[work_id]
        return len(deleted)

    def artist_has_match(self, artist_id: str) -> bool:
        return any(
            m.target_type == TargetType.ARTIST and m.target_id == artist_id
            for m in self._matches.values()
        )

    def delete_artist(self, artist_id: str) -> None:
        self._artists.pop(artist_id, None)

    # --- split and reassign --------------------------------------------------------------

    def insert_recording(self, recording: Recording) -> None:
        self._recordings[recording.id] = replace(recording, needs_enhancement=False)

    def set_file_recording(self, file_id: UUID, recording_id: str) -> None:
        file = self._files.get(file_id)
        if file is not None:
            self._files[file_id] = replace(file, recording_id=recording_id)

    def count_other_files_on_recording(self, recording_id: str, file_id: UUID) -> int:
        return sum(
            1 for f in self._files.values() if f.recording_id == recording_id and f.id != file_id
        )

    def store_split_master(self, master_id: UUID, work_id: str, file_id: UUID) -> None:
        self._masters[work_id] = SongMaster(
            id=master_id,
            work_id=work_id,
            preferred_file_id=file_id,
            selection_method=SelectionMethod.AUTO,
            score=None,
        )

    # --- a work a file left ----------------------------------------------------------------

    def release_work_matches(self, work_id: str) -> None:
        for match in [m for m in self._matches.values() if m.work_id == work_id]:
            file = self._files.get(match.library_file_id) if match.library_file_id else None
            self._matches[match.id] = replace(
                match, work_id=file.work_id if file is not None else None
            )

    def delete_overrides_of_work(self, work_id: str) -> None:
        for override_id in [o.id for o in self._overrides.values() if o.work_id == work_id]:
            del self._overrides[override_id]

    def delete_recordings_of_work(self, work_id: str) -> None:
        for recording_id in [r.id for r in self._recordings.values() if r.work_id == work_id]:
            del self._recordings[recording_id]

    # --- the master recalculation ------------------------------------------------------------

    def present_master_candidates(self, work_id: str) -> list[MasterCandidate]:
        return [
            MasterCandidate(
                id=f.id,
                release_status=f.audio.release_status.value if f.audio.release_status else None,
                release_type=f.audio.release_type.value if f.audio.release_type else None,
                format=f.format,
                bitrate=f.audio.bitrate,
                duration_ms=f.audio.duration_ms,
            )
            for f in self._files.values()
            if f.work_id == work_id and f.file_status == FileStatus.PRESENT
        ]

    def delete_master_off_work(self, work_id: str) -> None:
        master = self._masters.get(work_id)
        if master is None:
            return
        chosen = self._files.get(master.preferred_file_id)
        if chosen is None or chosen.work_id != work_id:
            del self._masters[work_id]

    def store_auto_master(self, master_id: UUID, work_id: str, file_id: UUID, score: int) -> None:
        stored = self._masters.get(work_id)
        self._masters[work_id] = SongMaster(
            id=stored.id if stored is not None else master_id,
            work_id=work_id,
            preferred_file_id=file_id,
            selection_method=SelectionMethod.AUTO,
            score=score,
        )

    # --- format overrides ------------------------------------------------------------------

    def insert_override_if_absent(self, override: FormatOverride) -> bool:
        if any(
            o.work_id == override.work_id and o.format_name == override.format_name
            for o in self._overrides.values()
        ):
            return False
        self._overrides[override.id] = override
        return True
