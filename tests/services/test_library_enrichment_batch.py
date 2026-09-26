"""enrich_by_recording_batch: link many pending files from one recording search.

The batch links every file whose recording came back and lists the file's
release; everything else is left pending, untouched, for the per-release
path to handle exactly as it does today.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import enrich_by_recording_batch
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recordings import FakeRecordingRepository

_RELEASE = "00000000-0000-4000-8000-000000000001"
_OTHER_RELEASE = "00000000-0000-4000-8000-000000000009"
_REC_A = "00000000-0000-4000-8000-00000000000a"
_REC_B = "00000000-0000-4000-8000-00000000000b"
_ARTIST = "00000000-0000-4000-8000-000000000003"


def _search_hit(rec_mbid: str, title: str, *releases: str) -> dict[str, Any]:
    return {
        "id": rec_mbid,
        "title": title,
        "length": 180000,
        "artist-credit": [{
            "name": "Track Artist",
            "artist": {"id": _ARTIST, "name": "Track Artist", "sort-name": "Artist, Track"},
        }],
        "releases": [{"id": r, "title": "Some Release"} for r in releases],
    }


def _pending(recording_mbid: str | None, release_mbid: str | None = _RELEASE) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.flac",
        file_hash="abc123",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(release_mbid=release_mbid, recording_mbid=recording_mbid),
    )


class _Repos:
    def __init__(self, client: FakeMbClient) -> None:
        self.files = FakeLibraryFileRepository()
        self.recordings = FakeRecordingRepository()
        self.artists = FakeArtistRepository()
        self.client = client

    def run(self, pending: list[LibraryFile]) -> Any:
        for f in pending:
            self.files.upsert(f)
        return enrich_by_recording_batch(
            pending, self.files, self.recordings, self.artists, self.client,
        )


def test_links_files_whose_recording_lists_their_release() -> None:
    repos = _Repos(FakeMbClient(recordings={
        _REC_A: _search_hit(_REC_A, "Song A (Live)", _RELEASE, _OTHER_RELEASE),
    }))
    lf = _pending(_REC_A)

    outcome = repos.run([lf])

    assert outcome.enriched == 1
    assert outcome.unresolved == ()
    updated = repos.files.get_by_id(lf.id)
    assert updated is not None
    assert updated.recording_id == _REC_A
    assert updated.enrichment_status == EnrichmentStatus.ENRICHED
    rec = repos.recordings.get_by_id(_REC_A)
    assert rec is not None
    assert rec.title == "Song A (Live)"
    assert rec.duration_ms == 180000
    assert rec.version_type == "live"
    assert rec.work_id is None
    artist = repos.artists.get_by_id(_ARTIST)
    assert artist is not None
    assert artist.sort_name == "Artist, Track"


def test_missing_recording_is_left_pending_for_the_fallback() -> None:
    repos = _Repos(FakeMbClient(recordings={}))
    lf = _pending(_REC_A)

    outcome = repos.run([lf])

    assert outcome.enriched == 0
    assert outcome.unresolved == (lf,)
    updated = repos.files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.PENDING
    assert repos.recordings.get_by_id(_REC_A) is None


def test_recording_not_on_the_files_release_is_left_pending() -> None:
    repos = _Repos(FakeMbClient(recordings={
        _REC_A: _search_hit(_REC_A, "Song A", _OTHER_RELEASE),
    }))
    lf = _pending(_REC_A)

    outcome = repos.run([lf])

    assert outcome.unresolved == (lf,)
    updated = repos.files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.PENDING


def test_files_without_a_recording_mbid_are_not_searched() -> None:
    client = FakeMbClient(recordings={_REC_A: _search_hit(_REC_A, "Song A", _RELEASE)})
    repos = _Repos(client)
    no_mbid = _pending(None)
    with_mbid = _pending(_REC_A)

    outcome = repos.run([no_mbid, with_mbid])

    assert outcome.enriched == 1
    assert outcome.unresolved == (no_mbid,)
    assert client.calls == [f"search_recordings_by_mbids:{_REC_A}"]


def test_one_search_covers_every_distinct_recording_once() -> None:
    client = FakeMbClient(recordings={
        _REC_A: _search_hit(_REC_A, "Song A", _RELEASE),
        _REC_B: _search_hit(_REC_B, "Song B", _RELEASE),
    })
    repos = _Repos(client)

    outcome = repos.run([_pending(_REC_A), _pending(_REC_A), _pending(_REC_B)])

    assert outcome.enriched == 3
    assert client.calls == [f"search_recordings_by_mbids:{_REC_A},{_REC_B}"]
