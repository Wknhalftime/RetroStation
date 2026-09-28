"""Branch/characterisation tests for AUD-048 (gate 1).

`enrich_by_release` was C901-complex (12 > 10) with the weakest branch
coverage in the audit's top 10 (50/70 branches, 87.8% lines, 45 tests). This
file adds a test for every branch `--cov-branch` reported missing before the
refactor that:

- extracts the recording-map build into `_build_recording_map`
- extracts the per-file loop body into a helper
- folds the artist-credit block `enrich_by_recording` duplicates from
  `enrich_by_release` into the existing `_upsert_artist_from_credits` helper
  (see the PR description for the `git log -L` evidence that the two blocks
  always change together)

Every test here must pass unchanged before AND after that refactor.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    _extract_artist_from_credits,
    _extract_work_from_relations,
    _move_file_to_work,
    _upsert_recording_with_work,
    enrich_by_recording,
    enrich_by_recording_batch,
    enrich_by_release,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository

_RELEASE = "00000000-0000-4000-8000-000000000101"
_ARTIST = "00000000-0000-4000-8000-000000000102"
_REC_OK = "00000000-0000-4000-8000-000000000103"
_WORK = "00000000-0000-4000-8000-000000000104"
_MALFORMED = "not-a-uuid"
_UNKNOWN_REC = "00000000-0000-4000-8000-0000000000ee"


def _repos(
    files: FakeLibraryFileRepository,
    *,
    recordings: FakeRecordingRepository | None = None,
    works: FakeWorkRepository | None = None,
    artists: FakeArtistRepository | None = None,
    matches: FakeMatchRepository | None = None,
    song_masters: FakeSongMasterRepository | None = None,
) -> EnrichmentRepos:
    return EnrichmentRepos(
        files=files,
        enrichment_queries=files,
        recordings=recordings or FakeRecordingRepository(),
        works=works or FakeWorkRepository(),
        song_masters=song_masters or FakeSongMasterRepository(),
        matches=matches or FakeMatchRepository(),
        artists=artists or FakeArtistRepository(),
    )


def _pending_file(
    release_mbid: str | None = _RELEASE,
    recording_mbid: str | None = _REC_OK,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.flac",
        file_hash="abc123",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(release_mbid=release_mbid, recording_mbid=recording_mbid),
    )


def _recording(rec_id: str, title: str = "Track", **extra: Any) -> dict[str, Any]:
    return {"id": rec_id, "title": title, **extra}


def _release_data(
    *,
    artist_credit: list[dict[str, Any]] | None = None,
    media: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": _RELEASE,
        "title": "Test Release",
        "artist-credit": (
            artist_credit
            if artist_credit is not None
            else [{"artist": {"id": _ARTIST, "name": "Artist", "sort-name": "Artist, The"}}]
        ),
        "media": (
            media
            if media is not None
            else [{"tracks": [{"recording": _recording(_REC_OK, "Track")}]}]
        ),
    }


class _CountingMatchRepo(FakeMatchRepository):
    """Records how many times `move_to_work` actually ran a move."""

    def __init__(self) -> None:
        super().__init__()
        self.move_calls = 0

    def move_to_work(self, file_id: UUID, work_id: str | None) -> None:
        self.move_calls += 1
        super().move_to_work(file_id, work_id)


# --- _extract_artist_from_credits --------------------------------------------


def test_extract_artist_skips_credit_with_no_artist_field() -> None:
    credits = [{"joinphrase": "feat."}, {"artist": {"id": _ARTIST, "name": "Band"}}]
    assert _extract_artist_from_credits(credits) == (_ARTIST, "Band", "Band")


def test_extract_artist_skips_credit_missing_id_or_name() -> None:
    credits = [
        {"artist": {"name": "No Id"}},
        {"artist": {"id": _ARTIST, "name": "Band", "sort-name": "Band, The"}},
    ]
    assert _extract_artist_from_credits(credits) == (_ARTIST, "Band", "Band, The")


def test_extract_artist_returns_none_when_no_credit_is_usable() -> None:
    assert _extract_artist_from_credits([{"artist": {"name": "No Id"}}]) is None


# --- _extract_work_from_relations ---------------------------------------------


def test_extract_work_skips_relation_with_wrong_type() -> None:
    relations = [{"type": "cover"}, {"type": "performance", "work": {"id": _WORK, "title": "T"}}]
    assert _extract_work_from_relations(relations) == (_WORK, "T")


def test_extract_work_skips_performance_relation_without_work() -> None:
    relations = [
        {"type": "performance"},
        {"type": "performance", "work": {"id": _WORK, "title": "T"}},
    ]
    assert _extract_work_from_relations(relations) == (_WORK, "T")


def test_extract_work_skips_relation_missing_id_or_title() -> None:
    relations = [
        {"type": "performance", "work": {"title": "No Id"}},
        {"type": "performance", "work": {"id": _WORK, "title": "T"}},
    ]
    assert _extract_work_from_relations(relations) == (_WORK, "T")


def test_extract_work_returns_none_when_no_performance_relation() -> None:
    assert _extract_work_from_relations([{"type": "cover"}]) is None


# --- _upsert_recording_with_work -----------------------------------------------


def test_upsert_recording_with_work_skips_work_without_relations() -> None:
    recordings = FakeRecordingRepository()
    repos = _repos(FakeLibraryFileRepository(), recordings=recordings)

    work_id = _upsert_recording_with_work(_REC_OK, _recording(_REC_OK, "Track"), _ARTIST, repos)

    assert work_id is None
    stored = recordings.get_by_id(_REC_OK)
    assert stored is not None
    assert stored.work_id is None


def test_upsert_recording_with_work_skips_work_without_artist() -> None:
    works = FakeWorkRepository()
    repos = _repos(FakeLibraryFileRepository(), works=works)
    rec_data = _recording(
        _REC_OK,
        "Track",
        relations=[{"type": "performance", "work": {"id": _WORK, "title": "Work"}}],
    )

    work_id = _upsert_recording_with_work(_REC_OK, rec_data, None, repos)

    assert work_id is None
    assert works.get_by_id(_WORK) is None


# --- _move_file_to_work ----------------------------------------------------------


def test_move_file_to_work_is_a_noop_when_already_on_the_target_work() -> None:
    files = FakeLibraryFileRepository()
    lf = _pending_file()
    lf.work_id = _WORK
    files.upsert(lf)
    matches = _CountingMatchRepo()
    repos = _repos(files, matches=matches)

    _move_file_to_work(lf, _WORK, repos)

    assert matches.move_calls == 0


# --- enrich_by_release: guard exits and loop-body branches ----------------------


def test_enrich_by_release_returns_early_when_nothing_pending() -> None:
    files = FakeLibraryFileRepository()
    mb_client = FakeMbClient(releases={_RELEASE: _release_data()})

    count = enrich_by_release(_RELEASE, _repos(files), mb_client)

    assert count == 0
    assert mb_client.calls == []


def test_enrich_by_release_without_artist_credit_still_links_recording() -> None:
    """No artist-credit on the release: recording still links; no artist upserted.

    The recording also carries no work relation, so this doubles as coverage
    for the work_id-is-None branch of the per-file loop (the move-to-work
    step is skipped).
    """
    files = FakeLibraryFileRepository()
    artists = FakeArtistRepository()
    lf = _pending_file()
    files.upsert(lf)
    mb_client = FakeMbClient(releases={_RELEASE: _release_data(artist_credit=[])})

    count = enrich_by_release(_RELEASE, _repos(files, artists=artists), mb_client)

    assert count == 1
    assert artists.list_all() == []
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.ENRICHED
    assert updated.work_id is None


def test_enrich_by_release_skips_tracks_without_a_usable_recording() -> None:
    files = FakeLibraryFileRepository()
    lf = _pending_file()
    files.upsert(lf)
    media = [
        {
            "tracks": [
                {},  # a track with no "recording" at all
                {"recording": {"title": "No Id"}},  # a recording with no "id"
                {"recording": _recording(_REC_OK, "Track")},
            ]
        }
    ]
    mb_client = FakeMbClient(releases={_RELEASE: _release_data(media=media)})

    count = enrich_by_release(_RELEASE, _repos(files), mb_client)

    assert count == 1
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.recording_id == _REC_OK


def test_enrich_by_release_fails_a_file_with_no_recording_mbid_tag() -> None:
    files = FakeLibraryFileRepository()
    lf = _pending_file(recording_mbid=None)
    files.upsert(lf)
    mb_client = FakeMbClient(releases={_RELEASE: _release_data()})

    count = enrich_by_release(_RELEASE, _repos(files), mb_client)

    assert count == 0
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED


def test_enrich_by_release_fails_a_malformed_recording_mbid_without_a_lookup() -> None:
    """A per-file recording_mbid tag can be corrupt even when the release_mbid is fine."""
    files = FakeLibraryFileRepository()
    lf = _pending_file(recording_mbid=_MALFORMED)
    files.upsert(lf)
    mb_client = FakeMbClient(releases={_RELEASE: _release_data()})

    count = enrich_by_release(_RELEASE, _repos(files), mb_client)

    assert count == 0
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED
    assert not any(c.startswith(f"lookup_recording:{_MALFORMED}") for c in mb_client.calls)


def test_enrich_by_release_fails_when_the_tagged_recording_is_wholly_unknown() -> None:
    """A well-formed recording_mbid, not on the release, that MB has no record of at all."""
    files = FakeLibraryFileRepository()
    lf = _pending_file(recording_mbid=_UNKNOWN_REC)
    files.upsert(lf)
    mb_client = FakeMbClient(releases={_RELEASE: _release_data()})  # no recordings configured

    count = enrich_by_release(_RELEASE, _repos(files), mb_client)

    assert count == 0
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.FAILED
    assert f"lookup_recording:{_UNKNOWN_REC}" in mb_client.calls


def test_enrich_by_release_mixed_batch_enriches_some_and_fails_others() -> None:
    files = FakeLibraryFileRepository()
    enriched_lf = _pending_file()
    no_mbid_lf = _pending_file(recording_mbid=None)
    unknown_lf = _pending_file(recording_mbid=_UNKNOWN_REC)
    for lf in (enriched_lf, no_mbid_lf, unknown_lf):
        files.upsert(lf)
    mb_client = FakeMbClient(releases={_RELEASE: _release_data()})

    count = enrich_by_release(_RELEASE, _repos(files), mb_client)

    assert count == 1
    enriched = files.get_by_id(enriched_lf.id)
    no_mbid = files.get_by_id(no_mbid_lf.id)
    unknown = files.get_by_id(unknown_lf.id)
    assert enriched is not None and enriched.enrichment_status == EnrichmentStatus.ENRICHED
    assert no_mbid is not None and no_mbid.enrichment_status == EnrichmentStatus.FAILED
    assert unknown is not None and unknown.enrichment_status == EnrichmentStatus.FAILED


# --- enrich_by_recording: guard exits and loop-body branches --------------------


def test_enrich_by_recording_returns_early_when_nothing_pending() -> None:
    files = FakeLibraryFileRepository()
    mb_client = FakeMbClient(recordings={_REC_OK: _recording(_REC_OK)})

    count = enrich_by_recording(_REC_OK, _repos(files), mb_client)

    assert count == 0
    assert mb_client.calls == []


def test_enrich_by_recording_without_artist_credit_still_links_file() -> None:
    """No artist-credit on the recording, and no work relation either.

    Covers both the no-artist-credit branch and the work_id-is-None branch
    of `enrich_by_recording`'s per-file loop in one pass.
    """
    files = FakeLibraryFileRepository()
    artists = FakeArtistRepository()
    lf = _pending_file(release_mbid=None)
    files.upsert(lf)
    mb_client = FakeMbClient(recordings={_REC_OK: _recording(_REC_OK)})

    count = enrich_by_recording(_REC_OK, _repos(files, artists=artists), mb_client)

    assert count == 1
    assert artists.list_all() == []
    updated = files.get_by_id(lf.id)
    assert updated is not None
    assert updated.enrichment_status == EnrichmentStatus.ENRICHED
    assert updated.work_id is None


# --- enrich_by_recording_batch: the artist-credit helper's None path ------------


def test_batch_recording_without_artist_credit_still_links_file() -> None:
    files = FakeLibraryFileRepository()
    artists = FakeArtistRepository()
    lf = _pending_file(recording_mbid=_REC_OK)
    files.upsert(lf)
    hit = _recording(_REC_OK, "Track", releases=[{"id": _RELEASE, "title": "R"}])
    mb_client = FakeMbClient(recordings={_REC_OK: hit})

    outcome = enrich_by_recording_batch([lf], _repos(files, artists=artists), mb_client)

    assert outcome.enriched == 1
    assert artists.list_all() == []
