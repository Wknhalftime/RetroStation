"""A recording lookup's payload links the work and the live version, not only the length.

The batched recording search that links most files carries no relations, so
those recordings get no work and a version read off the title alone. The
per-recording lookup the MB enrichment pass makes afterwards carries both
(a 2026-11-02 profile of the dev DB: 7,026 of 11,115 cached lookups name a
work the recording is not on, and 43 say live where "original" is stored).
`apply_recording_lookup` writes what the payload says, through the same
work move the per-release path uses.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.catalog import Recording
from backend.domain.enums import CatalogSource, EnrichmentStatus, VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    apply_recording_lookup,
    needs_recording_lookup,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_REC = "00000000-0000-4000-8000-000000000a01"
_OTHER_REC = "00000000-0000-4000-8000-000000000a02"
_ARTIST = "00000000-0000-4000-8000-000000000a03"
_WORK = "00000000-0000-4000-8000-000000000a04"

_CREDIT = [{"artist": {"id": _ARTIST, "name": "Aerosmith", "sort-name": "Aerosmith"}}]


def _performance(*attributes: str) -> dict[str, Any]:
    return {
        "type": "performance",
        "attributes": list(attributes),
        "work": {"id": _WORK, "title": "Dream On"},
    }


def _payload(
    title: str = "Dream On",
    *,
    length: int | None = 291000,
    relations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": _REC,
        "title": title,
        "artist-credit": _CREDIT,
        "relations": relations if relations is not None else [_performance()],
    }
    if length is not None:
        payload["length"] = length
    return payload


class _World:
    def __init__(self) -> None:
        self.files = FakeLibraryFileRepository()
        self.works = FakeWorkRepository()
        self.works.set_library_file_repo(self.files)
        self.recordings = FakeRecordingRepository()
        self.song_masters = FakeSongMasterRepository()
        self.song_masters.set_library_file_repo(self.files)
        self.repos = EnrichmentRepos(
            files=self.files,
            enrichment_queries=self.files,
            recordings=self.recordings,
            works=self.works,
            song_masters=self.song_masters,
            matches=FakeMatchRepository(),
            artists=FakeArtistRepository(),
        )
        self.local_work = self.works.create_local("Dream On", "local-artist")

    def recording(
        self,
        *,
        duration_ms: int | None = 291000,
        version_type: VersionType = VersionType.ORIGINAL,
        work_id: str | None = None,
    ) -> Recording:
        return self.recordings.upsert(
            Recording(
                id=_REC,
                title="Dream On",
                work_id=work_id,
                duration_ms=duration_ms,
                version_type=version_type,
            )
        )

    def file_on_local_work(self) -> LibraryFile:
        lf = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4()}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            recording_id=_REC,
            work_id=self.local_work,
            audio=AudioMetadata(recording_mbid=_REC),
        )
        self.files.upsert(lf)
        return lf

    def stored(self) -> Recording:
        rec = self.recordings.get_by_id(_REC)
        assert rec is not None
        return rec

    def work_of(self, lf: LibraryFile) -> str | None:
        stored = self.files.get_by_id(lf.id)
        assert stored is not None
        return stored.work_id


def test_links_the_performed_work_and_moves_the_files_onto_it() -> None:
    w = _World()
    rec = w.recording()
    files = [w.file_on_local_work(), w.file_on_local_work()]

    applied = apply_recording_lookup(rec, _payload(), w.repos)

    work = w.works.get_by_id(applied.work_id or "")
    assert work is not None
    assert work.mbid == _WORK
    assert work.origin == CatalogSource.MUSICBRAINZ
    assert applied.recording_linked is True
    assert w.stored().work_id == work.id
    assert applied.files_moved == 2
    assert all(w.work_of(lf) == work.id for lf in files)
    assert w.works.get_by_id(w.local_work) is None, "the emptied local work is settled"


def test_a_taken_version_slot_moves_the_files_but_leaves_the_recording_unlinked() -> None:
    # uq_recordings_work_version: one recording per (work, version). The
    # recording that holds the slot keeps it; the other's files still join
    # the work, which is what the work page and matching read.
    w = _World()
    rec = w.recording()
    lf = w.file_on_local_work()
    w.recordings.upsert(Recording(id=_OTHER_REC, title="Dream On", work_id=_WORK))
    w.works.upsert_from_mb(mbid=_WORK, title="Dream On", artist_id=_ARTIST)

    applied = apply_recording_lookup(rec, _payload(), w.repos)

    assert applied.work_id == _WORK
    assert applied.recording_linked is False
    assert w.stored().work_id is None
    assert applied.files_moved == 1
    assert w.work_of(lf) == _WORK


def test_a_recording_already_on_the_payloads_work_is_left_alone() -> None:
    w = _World()
    w.works.upsert_from_mb(mbid=_WORK, title="Dream On", artist_id=_ARTIST)
    rec = w.recording(work_id=_WORK)
    lf = w.file_on_local_work()
    w.files.update_work_id(lf.id, _WORK)

    applied = apply_recording_lookup(rec, _payload(), w.repos)

    assert applied.recording_linked is False
    assert applied.files_moved == 0
    assert w.stored().work_id == _WORK


def test_no_performance_relation_links_nothing() -> None:
    w = _World()
    rec = w.recording()
    lf = w.file_on_local_work()

    applied = apply_recording_lookup(rec, _payload(relations=[]), w.repos)

    assert applied.work_id is None
    assert applied.files_moved == 0
    assert w.work_of(lf) == w.local_work
    assert w.stored().work_id is None


def test_marks_the_recording_live_when_musicbrainz_says_so() -> None:
    w = _World()
    rec = w.recording()

    applied = apply_recording_lookup(rec, _payload(relations=[_performance("live")]), w.repos)

    assert applied.marked_live is True
    assert w.stored().version_type == VersionType.LIVE


def test_a_live_recording_stays_live_without_a_write() -> None:
    w = _World()
    rec = w.recording(version_type=VersionType.LIVE)

    applied = apply_recording_lookup(rec, _payload(relations=[_performance("live")]), w.repos)

    assert applied.marked_live is False
    assert w.stored().version_type == VersionType.LIVE


def test_a_version_difference_other_than_live_is_not_written() -> None:
    # Only the live flag is trusted over what the title said when the
    # recording was stored; "Dream On (Radio Edit)" would otherwise flip
    # an "original" to radio_edit here.
    w = _World()
    rec = w.recording()

    applied = apply_recording_lookup(rec, _payload("Dream On (Radio Edit)"), w.repos)

    assert applied.marked_live is False
    assert w.stored().version_type == VersionType.ORIGINAL


def test_a_payload_with_null_text_fields_is_read_as_plain() -> None:
    # A cached payload is JSON; a null title or disambiguation must not
    # crash the pass, it just says nothing about the version.
    w = _World()
    rec = w.recording()
    payload = _payload()
    payload["title"] = None
    payload["disambiguation"] = None

    applied = apply_recording_lookup(rec, payload, w.repos)

    assert applied.marked_live is False
    assert applied.recording_linked is True
    assert w.stored().version_type == VersionType.ORIGINAL


def test_fills_a_missing_duration_and_leaves_a_known_one() -> None:
    w = _World()
    missing = w.recording(duration_ms=None)

    applied = apply_recording_lookup(missing, _payload(length=291000), w.repos)

    assert applied.duration_set is True
    assert w.stored().duration_ms == 291000

    known = w.recording(duration_ms=200000)
    applied = apply_recording_lookup(known, _payload(length=291000), w.repos)

    assert applied.duration_set is False
    assert w.stored().duration_ms == 200000


def test_needs_lookup_when_the_duration_or_the_musicbrainz_work_is_missing() -> None:
    on_mb_work = Recording(id=_REC, title="t", work_id="w-mb", duration_ms=1)
    on_local_work = Recording(id=_REC, title="t", work_id="w-local", duration_ms=1)
    on_no_work = Recording(id=_REC, title="t", work_id=None, duration_ms=1)
    no_duration = Recording(id=_REC, title="t", work_id="w-mb", duration_ms=None)
    mb_works = {"w-mb"}

    assert needs_recording_lookup(on_mb_work, mb_works) is False
    assert needs_recording_lookup(on_local_work, mb_works) is True
    assert needs_recording_lookup(on_no_work, mb_works) is True
    assert needs_recording_lookup(no_duration, mb_works) is True
