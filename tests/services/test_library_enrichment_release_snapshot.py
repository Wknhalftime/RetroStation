"""Syrupy characterisation test for `enrich_by_release` (AUD-048 gate 1).

Drives `enrich_by_release` over one realistic multi-medium release with a
mixed batch of pending files (a straightforward link, a recording with no
work relation, a recording MusicBrainz has merged into another, a file with
no recording_mbid tag, a file with a malformed recording_mbid tag, and a file
whose recording_mbid MusicBrainz has no record of at all), and snapshots
every observable outcome: file statuses and recording links, the upserted
artist/recordings/works, the file->work move (and the emptied local work's
deletion), and the MusicBrainz client calls made.

Must stay byte-identical across the AUD-048 refactor (recording-map build
and per-file loop extracted into helpers; the artist-credit block folded
into `_upsert_artist_from_credits`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from backend.domain.curation import SongMaster
from backend.domain.enums import EnrichmentStatus, SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import EnrichmentRepos, enrich_by_release
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_RELEASE = "00000000-0000-4000-8000-000000000201"
_ARTIST_MBID = "00000000-0000-4000-8000-000000000202"
_REC_A = "00000000-0000-4000-8000-000000000210"
_REC_B = "00000000-0000-4000-8000-000000000211"
_REC_C_OLD = "00000000-0000-4000-8000-000000000212"
_REC_C_SURVIVOR = "00000000-0000-4000-8000-000000000213"
_WORK_A = "00000000-0000-4000-8000-000000000220"
_WORK_C = "00000000-0000-4000-8000-000000000221"
_UNKNOWN_REC = "00000000-0000-4000-8000-0000000000ff"

_FILE_A = UUID("00000000-0000-4000-8000-0000000000a1")
_FILE_B = UUID("00000000-0000-4000-8000-0000000000a2")
_FILE_C_MERGED = UUID("00000000-0000-4000-8000-0000000000a3")
_FILE_NO_MBID = UUID("00000000-0000-4000-8000-0000000000a4")
_FILE_MALFORMED = UUID("00000000-0000-4000-8000-0000000000a5")
_FILE_UNKNOWN = UUID("00000000-0000-4000-8000-0000000000a6")
_SONG_MASTER_ID = UUID("00000000-0000-4000-8000-0000000000b1")

_LABELS = {
    _FILE_A: "file_a_linked_and_moved",
    _FILE_B: "file_b_no_work_relation",
    _FILE_C_MERGED: "file_c_merged_recording",
    _FILE_NO_MBID: "file_no_recording_mbid",
    _FILE_MALFORMED: "file_malformed_recording_mbid",
    _FILE_UNKNOWN: "file_unknown_recording",
}

_RELEASE_DATA: dict[str, Any] = {
    "id": _RELEASE,
    "title": "Retro Album",
    "artist-credit": [
        {"artist": {"id": _ARTIST_MBID, "name": "Retro Band", "sort-name": "Band, Retro"}}
    ],
    "media": [
        {
            "position": 1,
            "tracks": [
                {
                    "recording": {
                        "id": _REC_A,
                        "title": "Song A",
                        "length": 200000,
                        "relations": [
                            {
                                "type": "performance",
                                "work": {"id": _WORK_A, "title": "Work A"},
                            }
                        ],
                    }
                },
                {"recording": {"id": _REC_B, "title": "Song B (Live)", "length": 300000}},
                {},  # an enhanced-CD data track: no recording at all
            ],
        },
        {
            "position": 2,
            "tracks": [
                {
                    "recording": {
                        "id": _REC_C_SURVIVOR,
                        "title": "Song C",
                        "length": 250000,
                        "relations": [
                            {
                                "type": "performance",
                                "work": {"id": _WORK_C, "title": "Work C"},
                            }
                        ],
                    }
                },
            ],
        },
    ],
}


def _pending_file(
    file_id: UUID, recording_mbid: str | None, work_id: str | None = None
) -> LibraryFile:
    return LibraryFile(
        id=file_id,
        file_path=f"/music/{file_id}.flac",
        file_hash=f"hash-{file_id}",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(release_mbid=_RELEASE, recording_mbid=recording_mbid),
        work_id=work_id,
    )


def test_enrich_by_release_over_a_multi_medium_release_with_mixed_files(
    snapshot: SnapshotAssertion,
) -> None:
    files = FakeLibraryFileRepository()
    recordings = FakeRecordingRepository()
    works = FakeWorkRepository()
    works.set_library_file_repo(files)
    artists = FakeArtistRepository()
    song_masters = FakeSongMasterRepository()
    matches = FakeMatchRepository()

    local_work_a = works.create_local("Song A", "local-artist-placeholder")
    files.upsert(_pending_file(_FILE_A, _REC_A, work_id=local_work_a))
    song_masters.upsert(
        SongMaster(
            id=_SONG_MASTER_ID,
            work_id=local_work_a,
            preferred_file_id=_FILE_A,
            selection_method=SelectionMethod.AUTO,
        )
    )
    files.upsert(_pending_file(_FILE_B, _REC_B))
    files.upsert(_pending_file(_FILE_C_MERGED, _REC_C_OLD))
    files.upsert(_pending_file(_FILE_NO_MBID, None))
    files.upsert(_pending_file(_FILE_MALFORMED, "not-a-uuid"))
    files.upsert(_pending_file(_FILE_UNKNOWN, _UNKNOWN_REC))

    mb_client = FakeMbClient(
        releases={_RELEASE: _RELEASE_DATA},
        # _REC_C_OLD is a recording MusicBrainz has since merged into _REC_C_SURVIVOR;
        # a direct lookup follows the redirect. _UNKNOWN_REC is configured nowhere,
        # so its lookup answers None.
        recordings={_REC_C_OLD: {"id": _REC_C_SURVIVOR, "title": "Song C"}},
    )

    repos = EnrichmentRepos(
        files=files,
        enrichment_queries=files,
        recordings=recordings,
        works=works,
        song_masters=song_masters,
        matches=matches,
        artists=artists,
    )

    count = enrich_by_release(_RELEASE, repos, mb_client)

    file_states = {}
    for file_id, label in _LABELS.items():
        stored = files.get_by_id(file_id)
        assert stored is not None
        file_states[label] = {
            "status": stored.enrichment_status.value,
            "recording_id": stored.recording_id,
            "work_id": stored.work_id,
        }

    artist_states = {
        artist.id: {
            "name": artist.name,
            "sort_name": artist.sort_name,
            "mbid": artist.mbid,
            "origin": artist.origin.value,
        }
        for artist in artists.list_all()
    }

    recording_states = {
        rec_id: {
            "title": rec.title,
            "work_id": rec.work_id,
            "duration_ms": rec.duration_ms,
            "version_type": rec.version_type.value,
        }
        for rec_id in (_REC_A, _REC_B, _REC_C_SURVIVOR)
        if (rec := recordings.get_by_id(rec_id)) is not None
    }

    work_states = {
        work_id: {"title": work.title, "artist_id": work.artist_id, "origin": work.origin.value}
        for work_id in (_WORK_A, _WORK_C)
        if (work := works.get_by_id(work_id)) is not None
    }

    work_a_master = song_masters.get_by_work(_WORK_A)
    work_a_master_state = (
        {
            "preferred_file": _LABELS.get(work_a_master.preferred_file_id),
            "selection_method": work_a_master.selection_method.value,
        }
        if work_a_master is not None
        else None
    )

    result = {
        "return_value": count,
        "files": file_states,
        "artists": artist_states,
        "recordings": recording_states,
        "works": work_states,
        "local_work_a_deleted": works.get_by_id(local_work_a) is None,
        "work_a_song_master": work_a_master_state,
        "mb_client_calls": mb_client.calls,
    }

    assert result == snapshot
