import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import AUDIO_HASHABLE_FORMATS, AudioHash, LibraryFile
from backend.repositories.library_file_enrichment import LibraryFileEnrichmentRepository
from backend.repositories.library_files import LibraryFileRepository


def _stat_unchanged(existing: LibraryFile, incoming: LibraryFile) -> bool:
    """Mirrors the PG upsert's size + mtime test, including its NULL semantics."""
    return (
        existing.file_size is not None
        and existing.file_mtime_ns is not None
        and (existing.file_size, existing.file_mtime_ns)
        == (incoming.file_size, incoming.file_mtime_ns)
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FakeLibraryFileRepository(LibraryFileRepository, LibraryFileEnrichmentRepository):
    def __init__(self, clock: Callable[[], datetime] = _utcnow) -> None:
        self._data: dict[UUID, LibraryFile] = {}
        self._clock = clock

    def upsert(self, file: LibraryFile) -> LibraryFile:
        existing = self.get_by_path(file.file_path)
        if existing is None:
            self._data[file.id] = file
            return file
        # Pg updates the stored row, never the caller's object: the caller
        # keeps its fresh id and its (absent) links.
        stored = dataclasses.replace(file)
        unchanged = _stat_unchanged(existing, file)
        if unchanged:
            stored.enrichment_status = existing.enrichment_status
            # D11 (spec 2026-10-05 §4.2): an unchanged file keeps its indexed_at.
            stored.indexed_at = existing.indexed_at
        if file.audio_hash is None and unchanged:
            stored.audio_hash = existing.audio_hash
        # Mirrors the PG COALESCE: a fresh extraction carries no links,
        # and must not erase the ones grouping/enrichment already built.
        if file.work_id is None:
            stored.work_id = existing.work_id
        if file.recording_id is None:
            stored.recording_id = existing.recording_id
        stored.file_status = FileStatus.PRESENT
        stored.missing_since = None
        # ON CONFLICT (file_path) updates the row in place: its id stays.
        stored.id = existing.id
        self._data[existing.id] = stored
        return stored

    def upsert_write_only(self, file: LibraryFile) -> None:
        self.upsert(file)

    def get_by_id(self, file_id: UUID) -> LibraryFile | None:
        return self._data.get(file_id)

    def get_by_ids(self, ids: list[UUID]) -> list[LibraryFile]:
        return [self._data[i] for i in ids if i in self._data]

    def normalized_artist_names_changed_since(self, when: datetime) -> set[str]:
        return {
            name
            for f in self._data.values()
            if (name := f.audio.normalized_artist_name)
            and (f.indexed_at > when or (f.missing_since is not None and f.missing_since > when))
        }

    def get_by_path(self, file_path: str) -> LibraryFile | None:
        return next((f for f in self._data.values() if f.file_path == file_path), None)

    def get_by_recording(self, recording_id: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.recording_id == recording_id]

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.audio.artist_mbid == artist_mbid]

    def get_by_normalized_artist_name(
        self,
        normalized_name: str,
        limit: int = 100,
    ) -> list[LibraryFile]:
        if not normalized_name:
            return []
        hits = [
            f
            for f in self._data.values()
            if f.audio.normalized_artist_name == normalized_name
            and f.file_status == FileStatus.PRESENT
        ]
        return sorted(hits, key=lambda f: str(f.id))[:limit]

    def get_by_recording_mbid(self, recording_mbid: str) -> list[LibraryFile]:
        return [
            f
            for f in self._data.values()
            if f.audio.recording_mbid == recording_mbid and f.file_status == FileStatus.PRESENT
        ]

    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]:
        return [
            f
            for f in self._data.values()
            if f.audio.release_mbid == release_mbid
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]:
        return [
            f
            for f in self._data.values()
            if f.audio.recording_mbid == recording_mbid
            and f.audio.release_mbid is None
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def get_pending_enrichment_with_release(self) -> list[LibraryFile]:
        return sorted(
            (
                f
                for f in self._data.values()
                if f.enrichment_status == EnrichmentStatus.PENDING
                and f.audio.release_mbid is not None
                and f.audio.recording_mbid is not None
            ),
            key=lambda f: f.file_path,
        )

    def update_recording_link(
        self, file_id: UUID, recording_id: str | None, enrichment_status: EnrichmentStatus
    ) -> None:
        if f := self._data.get(file_id):
            f.recording_id = recording_id
            f.enrichment_status = enrichment_status

    def count_by_format(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._data.values():
            counts[f.format] = counts.get(f.format, 0) + 1
        return counts

    def count_by_enrichment_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._data.values():
            key = f.enrichment_status.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]:
        # Normalise to a Path so the separator check works on both POSIX and Windows.
        folder = Path(folder_path)
        result = []
        for f in self._data.values():
            try:
                rel = Path(f.file_path).relative_to(folder)
            except ValueError:
                continue
            # Only direct children (no sub-directory components)
            if len(rel.parts) == 1:
                result.append(f)
        return result

    def get_path_statuses_under(self, root: str) -> dict[str, FileStatus]:
        base = Path(root)
        return {
            f.file_path: f.file_status
            for f in self._data.values()
            if base in Path(f.file_path).parents
        }

    def mark_missing(self, file_path: str) -> None:
        for f in self._data.values():
            if f.file_path == file_path:
                if f.file_status != FileStatus.MISSING:
                    f.missing_since = self._clock()
                f.file_status = FileStatus.MISSING
                break

    def relocate(self, file_id: UUID, new_path: str) -> None:
        if file_id in self._data:
            self._data[file_id] = dataclasses.replace(
                self._data[file_id],
                file_path=new_path,
                file_status=FileStatus.PRESENT,
                missing_since=None,
            )

    def update_work_id(self, file_id: UUID, work_id: str | None) -> None:
        if file_id in self._data:
            self._data[file_id] = dataclasses.replace(
                self._data[file_id],
                work_id=work_id,
            )

    def get_by_work(self, work_id: str) -> list[LibraryFile]:
        return sorted(
            (f for f in self._data.values() if f.work_id == work_id),
            key=lambda f: str(f.id),
        )

    def get_missing(self) -> list[LibraryFile]:
        return sorted(
            (f for f in self._data.values() if f.file_status == FileStatus.MISSING),
            key=lambda f: f.file_path,
        )

    def get_present_by_track(
        self,
        normalized_artist_name: str,
        track_number: int,
        normalized_title: str,
    ) -> list[LibraryFile]:
        key = (normalized_artist_name, track_number, normalized_title)
        return sorted(
            (
                f
                for f in self._data.values()
                if f.file_status == FileStatus.PRESENT
                and (
                    f.audio.normalized_artist_name,
                    f.audio.track_number,
                    f.audio.normalized_title,
                )
                == key
            ),
            key=lambda f: f.file_path,
        )

    def get_by_path_ignoring_case(self, file_path: str) -> list[LibraryFile]:
        return sorted(
            (f for f in self._data.values() if f.file_path.lower() == file_path.lower()),
            key=lambda f: f.file_path,
        )

    def get_case_duplicate_groups(self) -> list[list[LibraryFile]]:
        groups: dict[str, list[LibraryFile]] = {}
        for f in sorted(self._data.values(), key=lambda f: f.file_path):
            groups.setdefault(f.file_path.lower(), []).append(f)
        return [g for g in groups.values() if len(g) > 1]

    def merge_into(self, source_id: UUID, target_id: UUID) -> None:
        # The fake holds no matches or masters; only the row itself goes.
        self._data.pop(source_id, None)

    def get_by_audio_hash(self, audio_hash: AudioHash) -> list[LibraryFile]:
        return sorted(
            (f for f in self._data.values() if f.audio_hash == audio_hash),
            key=lambda f: f.file_path,
        )

    def get_by_stat(self, file_size: int, file_mtime_ns: int) -> list[LibraryFile]:
        return sorted(
            (
                f
                for f in self._data.values()
                if (f.file_size, f.file_mtime_ns) == (file_size, file_mtime_ns)
            ),
            key=lambda f: f.file_path,
        )

    def _audio_backlog(self) -> list[LibraryFile]:
        return sorted(
            (
                f
                for f in self._data.values()
                if f.audio_hash is None
                and f.file_status == FileStatus.PRESENT
                and f.format in AUDIO_HASHABLE_FORMATS
            ),
            key=lambda f: f.file_path,
        )

    def get_audio_unhashed_after(self, after_path: str | None, limit: int) -> list[LibraryFile]:
        rows = [f for f in self._audio_backlog() if after_path is None or f.file_path > after_path]
        return rows[:limit]

    def set_audio_hash(
        self,
        file_id: UUID,
        audio_hash: AudioHash,
        file_size: int,
        file_mtime_ns: int,
    ) -> bool:
        f = self._data.get(file_id)
        if (
            f is None
            or f.audio_hash is not None
            or (f.file_size, f.file_mtime_ns) != (file_size, file_mtime_ns)
        ):
            return False
        self._data[file_id] = dataclasses.replace(f, audio_hash=audio_hash)
        return True

    def count_audio_unhashed(self) -> int:
        return len(self._audio_backlog())

    def has_any(self) -> bool:
        return bool(self._data)

    def delete_missing(self, file_id: UUID) -> bool:
        row = self._data.get(file_id)
        if row is None or row.file_status != FileStatus.MISSING:
            return False
        del self._data[file_id]
        return True

    def lock_missing(self, file_id: UUID) -> bool:
        # No concurrency in the fake: only the status check.
        row = self._data.get(file_id)
        return row is not None and row.file_status == FileStatus.MISSING

    def lock_fold_pair(self, missing_id: UUID, successor_id: UUID) -> bool:
        # No concurrency in the fake: only the status check.
        missing, successor = self._data.get(missing_id), self._data.get(successor_id)
        return (
            missing is not None
            and missing.file_status == FileStatus.MISSING
            and successor is not None
            and successor.file_status == FileStatus.PRESENT
        )

    def reset_failed_enrichments(self) -> int:
        count = 0
        for f in self._data.values():
            if f.enrichment_status == EnrichmentStatus.FAILED:
                f.enrichment_status = EnrichmentStatus.PENDING
                count += 1
        return count
