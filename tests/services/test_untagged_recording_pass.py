"""The untagged-file pass: one search per (artist, title), then the per-recording path.

A confident pick hands the recording MBID to the same step the per-recording
pass uses (``enrich_files_by_recording``), so the file gets its recording,
work, duration and live flag the normal way. An ambiguous or empty search
marks the file skipped and lists it; nothing is guessed.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile, UntaggedCandidate
from backend.services.library_enrichment_service import EnrichmentRepos
from backend.services.untagged_recording_service import (
    PickOutcome,
    enrich_untagged_pair,
    untagged_pairs,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_ARTIST = "f9776598-2689-41db-98d3-829f5510021c"
_REC = "00000000-0000-4000-8000-000000000c01"
_REC_2 = "00000000-0000-4000-8000-000000000c02"
_WORK = "00000000-0000-4000-8000-000000000c03"
_CREDIT = [{"artist": {"id": _ARTIST, "name": "Jesus Jones", "sort-name": "Jesus Jones"}}]


def _hit(mbid: str, length: int) -> dict[str, Any]:
    return {
        "id": mbid,
        "title": "Right Here, Right Now",
        "length": length,
        "artist-credit": _CREDIT,
    }


def _lookup(mbid: str) -> dict[str, Any]:
    return {
        "id": mbid,
        "title": "Right Here, Right Now",
        "length": 183_000,
        "artist-credit": _CREDIT,
        "relations": [
            {
                "type": "performance",
                "attributes": [],
                "work": {"id": _WORK, "title": "Right Here, Right Now"},
            }
        ],
    }


class _World:
    def __init__(self) -> None:
        self.files = FakeLibraryFileRepository()
        self.works = FakeWorkRepository()
        self.works.set_library_file_repo(self.files)
        self.artists = FakeArtistRepository()
        self.song_masters = FakeSongMasterRepository()
        self.song_masters.set_library_file_repo(self.files)
        self.repos = EnrichmentRepos(
            files=self.files,
            enrichment_queries=self.files,
            recordings=FakeRecordingRepository(),
            works=self.works,
            song_masters=self.song_masters,
            matches=FakeMatchRepository(),
            artists=self.artists,
        )
        artist_id = self.artists.upsert_musicbrainz_artist(
            _ARTIST, "Jesus Jones", "Jesus Jones", "jesus jones"
        )
        self.local_work = self.works.create_local("Right Here, Right Now", artist_id)

    def file(self, title: str, duration_ms: int = 183_000) -> LibraryFile:
        lf = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4()}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            work_id=self.local_work,
            audio=AudioMetadata(track_title=title, duration_ms=duration_ms),
        )
        return self.files.upsert(lf)

    def candidate(self, lf: LibraryFile) -> UntaggedCandidate:
        return UntaggedCandidate(file=lf, artist_mbid=_ARTIST)

    def stored(self, lf: LibraryFile) -> LibraryFile:
        got = self.files.get_by_id(lf.id)
        assert got is not None
        return got


def test_pairs_coalesce_files_by_artist_and_search_title() -> None:
    world = _World()
    a = world.file("Right Here, Right Now [Album Version]")
    b = world.file("Right Here, Right Now")
    c = world.file("International Bright Young Thing")

    pairs = untagged_pairs([world.candidate(a), world.candidate(b), world.candidate(c)])

    assert [(p.artist_mbid, p.title, len(p.files)) for p in pairs] == [
        (_ARTIST, "Right Here, Right Now", 2),
        (_ARTIST, "International Bright Young Thing", 1),
    ]


def test_confident_files_are_enriched_through_the_recording_lookup() -> None:
    world = _World()
    a = world.file("Right Here, Right Now [Album Version]")
    b = world.file("Right Here, Right Now")
    client = FakeMbClient(
        recording_searches={(_ARTIST, "Right Here, Right Now"): [_hit(_REC, 183_400)]},
        recordings={_REC: _lookup(_REC)},
    )
    (pair,) = untagged_pairs([world.candidate(a), world.candidate(b)])

    outcome = enrich_untagged_pair(pair, world.repos, client)

    assert outcome.enriched == 2 and outcome.skipped == ()
    assert client.calls == [
        f"search_recording:{_ARTIST}:Right Here, Right Now",
        f"lookup_recording:{_REC}",
    ], "one search for the pair, one lookup for the recording"
    for lf in (a, b):
        stored = world.stored(lf)
        assert stored.enrichment_status is EnrichmentStatus.ENRICHED
        assert stored.recording_id == _REC
        assert stored.work_id is not None
        moved_to = world.works.get_by_id(stored.work_id)
        assert moved_to is not None and moved_to.mbid == _WORK, "moved onto the lookup's work"
    recording = world.repos.recordings.get_by_id(_REC)
    assert recording is not None and recording.duration_ms == 183_000


def test_files_of_one_pair_can_pick_different_recordings_by_length() -> None:
    world = _World()
    album = world.file("Right Here, Right Now", duration_ms=183_000)
    edit = world.file("Right Here, Right Now [Edit]", duration_ms=160_000)
    client = FakeMbClient(
        recording_searches={
            (_ARTIST, "Right Here, Right Now"): [_hit(_REC, 183_200), _hit(_REC_2, 160_900)]
        },
        recordings={_REC: _lookup(_REC), _REC_2: _lookup(_REC_2)},
    )
    (pair,) = untagged_pairs([world.candidate(album), world.candidate(edit)])

    outcome = enrich_untagged_pair(pair, world.repos, client)

    assert outcome.enriched == 2
    assert world.stored(album).recording_id == _REC
    assert world.stored(edit).recording_id == _REC_2


def test_ambiguous_and_empty_files_are_skipped_and_listed_without_a_lookup() -> None:
    world = _World()
    ambiguous = world.file("Right Here, Right Now", duration_ms=183_000)
    empty = world.file("International Bright Young Thing")
    client = FakeMbClient(
        recording_searches={
            (_ARTIST, "Right Here, Right Now"): [_hit(_REC, 183_000), _hit(_REC_2, 184_000)]
        },
        recordings={_REC: _lookup(_REC), _REC_2: _lookup(_REC_2)},
    )
    pairs = untagged_pairs([world.candidate(ambiguous), world.candidate(empty)])

    outcomes = [enrich_untagged_pair(p, world.repos, client) for p in pairs]

    assert [o.enriched for o in outcomes] == [0, 0]
    assert [(s.file_path, s.outcome, s.candidate_mbids) for o in outcomes for s in o.skipped] == [
        (ambiguous.file_path, PickOutcome.AMBIGUOUS, (_REC, _REC_2)),
        (empty.file_path, PickOutcome.EMPTY, ()),
    ]
    for lf in (ambiguous, empty):
        stored = world.stored(lf)
        assert stored.enrichment_status is EnrichmentStatus.SKIPPED
        assert stored.recording_id is None
        assert stored.work_id == world.local_work
    assert not [c for c in client.calls if c.startswith("lookup_recording")]


def test_a_failed_lookup_fails_the_files_as_the_recording_pass_does() -> None:
    world = _World()
    lf = world.file("Right Here, Right Now")
    client = FakeMbClient(
        recording_searches={(_ARTIST, "Right Here, Right Now"): [_hit(_REC, 183_000)]},
    )
    (pair,) = untagged_pairs([world.candidate(lf)])

    outcome = enrich_untagged_pair(pair, world.repos, client)

    assert outcome.enriched == 0
    assert world.stored(lf).enrichment_status is EnrichmentStatus.FAILED
