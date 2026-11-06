"""Enrichment upserts every credited artist (phase 2a S8, D5; AUD-R073).

The first as ever, promoting a name-only artist when the name's MBIDs are none or only this one.
Each further credit is new, and a wrong MBID is worse than none (AUD-R026), so it promotes only
when the name's MBIDs are exactly this one, and with none it inserts. A special-purpose artist in
a further credit gets no artist (C7). The work keeps its first-credited artist until 3b.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS, Recording
from backend.domain.enums import CatalogSource, EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    apply_recording_lookup,
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

FIRST = "0a000000-0000-4000-8000-000000000001"
SECOND = "0a000000-0000-4000-8000-000000000002"
THIRD = "0a000000-0000-4000-8000-000000000003"
RECORDING = "0b000000-0000-4000-8000-000000000001"
RELEASE = "0c000000-0000-4000-8000-000000000001"
WORK = "0d000000-0000-4000-8000-000000000001"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]


def _credit(mbid: str, name: str, join: str = "") -> dict[str, Any]:
    # A real credit's own name can be a credited-as one: the artist's is what is upserted.
    return {
        "name": f"credited as {name}",
        "joinphrase": join,
        "artist": {"id": mbid, "name": name, "sort-name": f"{name}, The"},
    }


CREDITS = [_credit(FIRST, "First", " & "), _credit(SECOND, "Second")]


def _recording(credits: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": RECORDING,
        "title": "Song",
        "artist-credit": credits,
        "relations": [{"type": "performance", "work": {"id": WORK, "title": "Song"}}],
    }


def _repos(artists: FakeArtistRepository | None = None) -> EnrichmentRepos:
    files = FakeLibraryFileRepository()
    return EnrichmentRepos(
        files=files,
        enrichment_queries=files,
        recordings=FakeRecordingRepository(),
        works=FakeWorkRepository(),
        song_masters=FakeSongMasterRepository(),
        matches=FakeMatchRepository(),
        artists=artists or FakeArtistRepository(),
    )


def _file(repos: EnrichmentRepos, *, release: str | None = None) -> LibraryFile:
    file = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(recording_mbid=RECORDING, release_mbid=release, track_title="Song"),
    )
    return repos.files.upsert(file)


def _by_recording(repos: EnrichmentRepos, credits: list[dict[str, Any]]) -> None:
    _file(repos)
    client = FakeMbClient(recordings={RECORDING: _recording(credits)})
    enrich_by_recording(RECORDING, repos, client)


def _by_recording_batch(repos: EnrichmentRepos, credits: list[dict[str, Any]]) -> None:
    client = FakeMbClient(recordings={RECORDING: _recording(credits)})
    enrich_by_recording_batch([_file(repos)], repos, client)


def _by_release(repos: EnrichmentRepos, credits: list[dict[str, Any]]) -> None:
    _file(repos, release=RELEASE)
    release = {
        "id": RELEASE,
        "title": "Album",
        "artist-credit": credits,
        "media": [{"tracks": [{"recording": _recording(credits)}]}],
    }
    enrich_by_release(RELEASE, repos, FakeMbClient(releases={RELEASE: release}))


def _by_lookup(repos: EnrichmentRepos, credits: list[dict[str, Any]]) -> None:
    recording = Recording(id=RECORDING, title="Song")
    repos.recordings.upsert(recording)
    apply_recording_lookup(recording, _recording(credits), repos)  # type: ignore[arg-type]


CALLERS: list[Callable[[EnrichmentRepos, list[dict[str, Any]]], None]] = [
    _by_recording,
    _by_recording_batch,
    _by_release,
    _by_lookup,
]
CALLER_IDS = ["recording", "recording batch", "release", "recording lookup"]


@pytest.mark.parametrize("enrich", CALLERS, ids=CALLER_IDS)
def test_every_credited_artist_is_upserted(
    enrich: Callable[[EnrichmentRepos, list[dict[str, Any]]], None],
) -> None:
    repos = _repos()

    enrich(repos, CREDITS)

    for mbid, name in ((FIRST, "First"), (SECOND, "Second")):
        artist = repos.artists.get_by_mbid(mbid)
        assert artist is not None
        assert (artist.name, artist.sort_name) == (name, f"{name}, The")


@pytest.mark.parametrize("enrich", CALLERS, ids=CALLER_IDS)
def test_a_placeholder_in_a_further_credit_gets_no_artist(
    enrich: Callable[[EnrichmentRepos, list[dict[str, Any]]], None],
) -> None:
    assert UNKNOWN in SPECIAL_PURPOSE_MBIDS
    repos = _repos()

    enrich(repos, [_credit(FIRST, "First", " & "), _credit(UNKNOWN, "[unknown]")])

    assert repos.artists.get_by_mbid(FIRST) is not None
    assert repos.artists.get_by_mbid(UNKNOWN) is None


def test_the_work_keeps_its_first_credited_artist() -> None:
    repos = _repos()

    _by_recording(repos, CREDITS)

    first = repos.artists.get_by_mbid(FIRST)
    assert first is not None
    [work] = repos.works.get_by_artist(first.id)
    assert work.artist_id == first.id
    second = repos.artists.get_by_mbid(SECOND)
    assert second is not None
    assert repos.works.get_by_artist(second.id) == []


def test_three_credits() -> None:
    repos = _repos()

    _by_recording(repos, [*CREDITS, _credit(THIRD, "Third")])

    assert repos.artists.get_by_mbid(THIRD) is not None


def test_a_credit_with_no_artist_is_passed_by() -> None:
    repos = _repos()

    _by_recording(repos, [_credit(FIRST, "First"), {"name": "Nobody"}, _credit(SECOND, "Second")])

    assert repos.artists.get_by_mbid(SECOND) is not None


def test_a_credit_with_no_name_is_passed_by() -> None:
    repos = _repos()
    unnamed = {"artist": {"id": SECOND}}

    _by_recording(repos, [_credit(FIRST, "First"), unnamed])

    assert repos.artists.get_by_mbid(SECOND) is None


def test_a_repeated_credit_is_one_artist() -> None:
    repos = _repos()

    _by_recording(repos, [_credit(FIRST, "First"), _credit(FIRST, "First")])

    assert sum(a.mbid == FIRST for a in repos.artists.list_all()) == 1


def _name_only(artists: FakeArtistRepository, name: str, normalized: str) -> str:
    return artists.upsert_local_artist(name, normalized)


@pytest.mark.parametrize(
    ("tags", "promotes"),
    [(set(), True), ({FIRST}, True), ({THIRD}, False), ({FIRST, THIRD}, False)],
    ids=["no tags", "only this MBID", "another MBID", "several"],
)
def test_the_first_credit_promotes_as_s6_reads(tags: set[str], promotes: bool) -> None:
    artists = FakeArtistRepository()
    name_only = _name_only(artists, "First", "first")
    artists.file_mbids["first"] = tags
    repos = _repos(artists)

    _by_recording(repos, CREDITS)

    first = artists.get_by_mbid(FIRST)
    assert first is not None
    assert (first.id == name_only) is promotes


@pytest.mark.parametrize(
    ("tags", "promotes"),
    [(set(), False), ({SECOND}, True), ({THIRD}, False), ({SECOND, THIRD}, False)],
    ids=["no tags", "only this MBID", "another MBID", "several"],
)
def test_a_further_credit_promotes_only_on_exactly_this_mbid(
    tags: set[str], promotes: bool
) -> None:
    """D5: with no tag at all it inserts, leaving the name-only artist as it was."""
    artists = FakeArtistRepository()
    name_only = _name_only(artists, "Second", "second")
    artists.file_mbids["second"] = tags
    repos = _repos(artists)

    _by_recording(repos, CREDITS)

    second = artists.get_by_mbid(SECOND)
    assert second is not None
    assert (second.id == name_only) is promotes
    held = artists.get_by_id(name_only)
    assert held is not None
    assert (held.mbid == SECOND) is promotes
    assert (held.origin == CatalogSource.MUSICBRAINZ) is promotes


def test_a_further_credit_finds_its_mbids_artist() -> None:
    artists = FakeArtistRepository()
    held = artists.upsert_musicbrainz_artist(SECOND, "Second", "Second", "second")
    repos = _repos(artists)

    _by_recording(repos, CREDITS)

    assert artists.upsert_further_credit(SECOND, "Second", "Second", "second") == held
    assert sum(a.mbid == SECOND for a in artists.list_all()) == 1


def test_a_placeholder_first_credit_is_as_it_was() -> None:
    """Only further credits are new. The first stays as today, whatever it names."""
    repos = _repos()

    _by_recording(repos, [_credit(UNKNOWN, "[unknown]", " & "), _credit(SECOND, "Second")])

    assert repos.artists.get_by_mbid(UNKNOWN) is not None
    assert repos.artists.get_by_mbid(SECOND) is not None


def test_the_first_usable_credit_is_the_first() -> None:
    """A credit with no artist ahead of it changes nothing: First keeps the work, and S6's wider
    promotion, and Second is a further credit."""
    artists = FakeArtistRepository()
    first_only = _name_only(artists, "First", "first")
    second_only = _name_only(artists, "Second", "second")
    repos = _repos(artists)

    _by_recording(repos, [{"name": "Nobody"}, *CREDITS])

    first = artists.get_by_mbid(FIRST)
    second = artists.get_by_mbid(SECOND)
    assert first is not None
    assert second is not None
    assert first.id == first_only
    assert second.id != second_only
    assert [work.artist_id for work in repos.works.get_by_artist(first.id)] == [first.id]
