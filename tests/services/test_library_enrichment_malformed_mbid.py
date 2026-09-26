"""A corrupt MBID tag must fail its files permanently, not leave them pending.

MusicBrainz answers a malformed MBID with 400 "Invalid mbid." (not 404), which
the client raises. Before the fix that exception escaped enrich_by_release /
enrich_by_recording, the task rolled back, and the files stayed 'pending' —
so every enrichment run re-queried MusicBrainz for the same bad ID.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

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
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_MALFORMED_MBID = "not-a-uuid"
_VALID_RECORDING_MBID = "a3c5b4e2-1f0d-4c3b-9a8e-7d6f5e4c3b2a"


def _pending_file(
    release_mbid: str | None, recording_mbid: str | None
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.flac",
        file_hash="abc123",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(release_mbid=release_mbid, recording_mbid=recording_mbid),
    )


def _run_release(files: FakeLibraryFileRepository, mb_client: FakeMbClient, mbid: str) -> int:
    return enrich_by_release(
        mbid,
        files,
        files,
        FakeRecordingRepository(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
        FakeArtistRepository(),
        mb_client,
    )


def _run_recording(
    files: FakeLibraryFileRepository, mb_client: FakeMbClient, mbid: str
) -> int:
    return enrich_by_recording(
        mbid,
        files,
        files,
        FakeRecordingRepository(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
        FakeArtistRepository(),
        mb_client,
    )


def test_malformed_release_mbid_fails_files_instead_of_leaving_them_pending() -> None:
    files = FakeLibraryFileRepository()
    lf = _pending_file(release_mbid=_MALFORMED_MBID, recording_mbid=None)
    files.upsert(lf)
    mb_client = FakeMbClient(bad_request_mbids={_MALFORMED_MBID})

    count = _run_release(files, mb_client, _MALFORMED_MBID)

    assert count == 0
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED
    # The next enrichment run's pending query no longer picks the file up.
    assert files.get_pending_enrichment_by_release(_MALFORMED_MBID) == []


def test_malformed_recording_mbid_fails_files_instead_of_leaving_them_pending() -> None:
    files = FakeLibraryFileRepository()
    lf = _pending_file(release_mbid=None, recording_mbid=_MALFORMED_MBID)
    files.upsert(lf)
    mb_client = FakeMbClient(bad_request_mbids={_MALFORMED_MBID})

    count = _run_recording(files, mb_client, _MALFORMED_MBID)

    assert count == 0
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED
    assert files.get_pending_enrichment_by_recording(_MALFORMED_MBID) == []


def test_malformed_release_mbid_is_never_sent_to_musicbrainz() -> None:
    files = FakeLibraryFileRepository()
    files.upsert(_pending_file(release_mbid=_MALFORMED_MBID, recording_mbid=None))
    mb_client = FakeMbClient(bad_request_mbids={_MALFORMED_MBID})

    _run_release(files, mb_client, _MALFORMED_MBID)

    assert mb_client.calls == []


def test_malformed_recording_mbid_is_never_sent_to_musicbrainz() -> None:
    files = FakeLibraryFileRepository()
    files.upsert(_pending_file(release_mbid=None, recording_mbid=_MALFORMED_MBID))
    mb_client = FakeMbClient(bad_request_mbids={_MALFORMED_MBID})

    _run_recording(files, mb_client, _MALFORMED_MBID)

    assert mb_client.calls == []


def test_transient_error_on_valid_mbid_still_propagates_for_retry() -> None:
    """Guard rail: only malformed IDs fail permanently; transient errors are retried."""
    files = FakeLibraryFileRepository()
    lf = _pending_file(release_mbid=None, recording_mbid=_VALID_RECORDING_MBID)
    files.upsert(lf)
    mb_client = FakeMbClient(error_mbids={_VALID_RECORDING_MBID})

    with pytest.raises(httpx.ConnectError):
        _run_recording(files, mb_client, _VALID_RECORDING_MBID)

    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.PENDING
