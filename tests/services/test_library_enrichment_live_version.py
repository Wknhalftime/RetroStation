"""A recording MusicBrainz marks live is stored as a live version.

MusicBrainz titles a live recording with the plain song name ("Dream On") and
says it is live elsewhere: the recording's disambiguation ("live") and a
"live" attribute on its performance relation. Enrichment used to read the
version type off the title alone, so every such recording was stored as
"original" and the work page grouped live copies with the studio ones.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.domain.enums import EnrichmentStatus, VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
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

_RELEASE = "00000000-0000-4000-8000-000000000201"
_REC = "00000000-0000-4000-8000-000000000202"
_ARTIST = "00000000-0000-4000-8000-000000000203"
_WORK = "00000000-0000-4000-8000-000000000204"

_CREDIT = [{"artist": {"id": _ARTIST, "name": "Aerosmith", "sort-name": "Aerosmith"}}]


def _performance(*attributes: str) -> dict[str, Any]:
    return {
        "type": "performance",
        "attributes": list(attributes),
        "work": {"id": _WORK, "title": "Dream On"},
    }


def _recording(
    title: str = "Dream On",
    *,
    disambiguation: str = "",
    relations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": _REC,
        "title": title,
        "disambiguation": disambiguation,
        "length": 291000,
        "artist-credit": _CREDIT,
        "relations": relations if relations is not None else [_performance()],
        "releases": [{"id": _RELEASE, "title": "A Little South of Sanity"}],
    }


def _release(recording: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _RELEASE,
        "title": "A Little South of Sanity",
        "artist-credit": _CREDIT,
        "media": [{"position": 1, "tracks": [{"recording": recording}]}],
    }


class _Harness:
    def __init__(self) -> None:
        self.files = FakeLibraryFileRepository()
        self.recordings = FakeRecordingRepository()
        self.repos = EnrichmentRepos(
            files=self.files,
            enrichment_queries=self.files,
            recordings=self.recordings,
            works=FakeWorkRepository(),
            song_masters=FakeSongMasterRepository(),
            matches=FakeMatchRepository(),
            artists=FakeArtistRepository(),
        )

    def pending(self, release_mbid: str | None = _RELEASE) -> LibraryFile:
        lf = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4()}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            audio=AudioMetadata(release_mbid=release_mbid, recording_mbid=_REC),
        )
        self.files.upsert(lf)
        return lf

    def stored_version(self) -> VersionType:
        rec = self.recordings.get_by_id(_REC)
        assert rec is not None
        return rec.version_type


def _via_release(recording: dict[str, Any]) -> VersionType:
    h = _Harness()
    h.pending()
    enrich_by_release(_RELEASE, h.repos, FakeMbClient(releases={_RELEASE: _release(recording)}))
    return h.stored_version()


def _via_recording_lookup(recording: dict[str, Any]) -> VersionType:
    h = _Harness()
    h.pending(release_mbid=None)
    enrich_by_recording(_REC, h.repos, FakeMbClient(recordings={_REC: recording}))
    return h.stored_version()


def _via_batch_search(recording: dict[str, Any]) -> VersionType:
    # Search results carry no relations; only the disambiguation survives.
    hit = {k: v for k, v in recording.items() if k != "relations"}
    h = _Harness()
    lf = h.pending()
    enrich_by_recording_batch([lf], h.repos, FakeMbClient(recordings={_REC: hit}))
    return h.stored_version()


_LOOKUP_PATHS = [_via_release, _via_recording_lookup]
_ALL_PATHS = [*_LOOKUP_PATHS, _via_batch_search]


@pytest.mark.parametrize("enrich", _LOOKUP_PATHS)
def test_live_performance_attribute_marks_recording_live(enrich: Any) -> None:
    recording = _recording(relations=[_performance("live")])

    assert enrich(recording) == VersionType.LIVE


@pytest.mark.parametrize("enrich", _ALL_PATHS)
def test_live_disambiguation_marks_recording_live(enrich: Any) -> None:
    recording = _recording(
        "Another Brick in the Wall, Part II",
        disambiguation="live, Delicate Sound of Thunder version",
    )

    assert enrich(recording) == VersionType.LIVE


@pytest.mark.parametrize("enrich", _ALL_PATHS)
def test_studio_recording_stays_original(enrich: Any) -> None:
    assert enrich(_recording()) == VersionType.ORIGINAL


@pytest.mark.parametrize("enrich", _ALL_PATHS)
def test_non_live_disambiguation_does_not_change_the_version(enrich: Any) -> None:
    # "album version" would read as REMIX through the title-suffix rules;
    # only a live disambiguation is trusted.
    assert enrich(_recording(disambiguation="album version")) == VersionType.ORIGINAL


@pytest.mark.parametrize("enrich", _ALL_PATHS)
def test_live_outranks_another_version_in_the_title(enrich: Any) -> None:
    # A remastered live recording is still live: that is the distinction
    # rotation cares about.
    recording = _recording("Dream On (Remastered)", disambiguation="live")

    assert enrich(recording) == VersionType.LIVE


@pytest.mark.parametrize("enrich", _ALL_PATHS)
def test_version_in_the_title_applies_when_musicbrainz_does_not_say_live(enrich: Any) -> None:
    assert enrich(_recording("Dream On (Radio Edit)")) == VersionType.RADIO_EDIT
