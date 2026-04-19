from __future__ import annotations

from uuid import uuid4

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import (
    enrich_by_recording,
    enrich_by_release,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.works import FakeWorkRepository

_RELEASE_MBID = "release-mbid-0000-0000-0000-000000000001"
_RECORDING_MBID = "recording-mbid-0000-0000-0000-000000000001"
_ARTIST_MBID = "artist-mbid-0000-0000-0000-000000000001"
_WORK_MBID = "work-mbid-0000-0000-0000-000000000001"

_FAKE_RELEASE = {
    "id": _RELEASE_MBID,
    "title": "Test Album",
    "artist-credit": [
        {
            "artist": {
                "id": _ARTIST_MBID,
                "name": "Test Artist",
                "sort-name": "Artist, Test",
            }
        }
    ],
    "media": [
        {
            "tracks": [
                {
                    "recording": {
                        "id": _RECORDING_MBID,
                        "title": "Test Track",
                        "length": 240000,
                        "relations": [
                            {
                                "type": "performance",
                                "work": {
                                    "id": _WORK_MBID,
                                    "title": "Test Work",
                                },
                            }
                        ],
                    }
                }
            ]
        }
    ],
}


def _pending_file(
    release_mbid: str | None = _RELEASE_MBID,
    recording_mbid: str | None = _RECORDING_MBID,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.flac",
        file_hash="abc123",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(
            release_mbid=release_mbid,
            recording_mbid=recording_mbid,
        ),
    )


def test_enrich_by_release_links_recording() -> None:
    """A pending file with matching recording_mbid is linked and marked ENRICHED."""
    library_file_repo = FakeLibraryFileRepository()
    recording_repo = FakeRecordingRepository()
    work_repo = FakeWorkRepository()
    artist_repo = FakeArtistRepository()
    mb_client = FakeMbClient(releases={_RELEASE_MBID: _FAKE_RELEASE})

    lf = _pending_file()
    library_file_repo.upsert(lf)

    count = enrich_by_release(
        _RELEASE_MBID,
        library_file_repo,
        library_file_repo,
        recording_repo,
        work_repo,
        artist_repo,
        mb_client,
    )

    assert count == 1
    assert f"lookup_release:{_RELEASE_MBID}" in mb_client.calls

    # File linked and status updated
    updated = library_file_repo.get_by_id(lf.id)
    assert updated is not None
    assert updated.recording_id == _RECORDING_MBID
    assert updated.enrichment_status == EnrichmentStatus.ENRICHED

    # Recording created
    rec = recording_repo.get_by_id(_RECORDING_MBID)
    assert rec is not None
    assert rec.title == "Test Track"
    assert rec.duration_ms == 240000
    assert rec.work_id == _WORK_MBID

    # Work created
    work = work_repo.get_by_id(_WORK_MBID)
    assert work is not None
    assert work.title == "Test Work"
    assert work.artist_id == _ARTIST_MBID

    # Artist upserted
    artist = artist_repo.get_by_id(_ARTIST_MBID)
    assert artist is not None
    assert artist.name == "Test Artist"
    assert artist.sort_name == "Artist, Test"


def test_enrich_missing_release_marks_failed() -> None:
    """When MB returns None for a release, all pending files are marked FAILED."""
    library_file_repo = FakeLibraryFileRepository()
    recording_repo = FakeRecordingRepository()
    work_repo = FakeWorkRepository()
    artist_repo = FakeArtistRepository()
    mb_client = FakeMbClient()  # no releases configured → returns None

    lf = _pending_file()
    library_file_repo.upsert(lf)

    count = enrich_by_release(
        _RELEASE_MBID,
        library_file_repo,
        library_file_repo,
        recording_repo,
        work_repo,
        artist_repo,
        mb_client,
    )

    assert count == 0
    assert f"lookup_release:{_RELEASE_MBID}" in mb_client.calls

    updated = library_file_repo.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED

    # No recordings or artists created
    assert recording_repo.get_by_id(_RECORDING_MBID) is None
    assert artist_repo.get_by_id(_ARTIST_MBID) is None


def test_enrich_by_recording_links_file() -> None:
    """A pending file with no release_mbid is enriched via direct recording lookup."""
    library_file_repo = FakeLibraryFileRepository()
    recording_repo = FakeRecordingRepository()
    work_repo = FakeWorkRepository()
    artist_repo = FakeArtistRepository()

    fake_recording = {
        "id": _RECORDING_MBID,
        "title": "Direct Track",
        "length": 180000,
        "artist-credit": [
            {
                "artist": {
                    "id": _ARTIST_MBID,
                    "name": "Solo Artist",
                    "sort-name": "Artist, Solo",
                }
            }
        ],
        "relations": [
            {
                "type": "performance",
                "work": {
                    "id": _WORK_MBID,
                    "title": "Direct Work",
                },
            }
        ],
    }
    mb_client = FakeMbClient(recordings={_RECORDING_MBID: fake_recording})

    lf = _pending_file(release_mbid=None, recording_mbid=_RECORDING_MBID)
    library_file_repo.upsert(lf)

    count = enrich_by_recording(
        _RECORDING_MBID,
        library_file_repo,
        library_file_repo,
        recording_repo,
        work_repo,
        artist_repo,
        mb_client,
    )

    assert count == 1
    assert f"lookup_recording:{_RECORDING_MBID}" in mb_client.calls

    updated = library_file_repo.get_by_id(lf.id)
    assert updated is not None
    assert updated.recording_id == _RECORDING_MBID
    assert updated.enrichment_status == EnrichmentStatus.ENRICHED

    rec = recording_repo.get_by_id(_RECORDING_MBID)
    assert rec is not None
    assert rec.title == "Direct Track"
    assert rec.work_id == _WORK_MBID

    artist = artist_repo.get_by_id(_ARTIST_MBID)
    assert artist is not None
    assert artist.name == "Solo Artist"


def test_enrich_missing_recording_marks_failed() -> None:
    """When MB returns None for a recording, pending files are marked FAILED."""
    library_file_repo = FakeLibraryFileRepository()
    recording_repo = FakeRecordingRepository()
    work_repo = FakeWorkRepository()
    artist_repo = FakeArtistRepository()
    mb_client = FakeMbClient()  # no recordings configured → returns None

    lf = _pending_file(release_mbid=None, recording_mbid=_RECORDING_MBID)
    library_file_repo.upsert(lf)

    count = enrich_by_recording(
        _RECORDING_MBID,
        library_file_repo,
        library_file_repo,
        recording_repo,
        work_repo,
        artist_repo,
        mb_client,
    )

    assert count == 0
    updated = library_file_repo.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED
