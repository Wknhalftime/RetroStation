"""planned_work's exact-title pass takes grouping's tie order (AUD-R103, L2).

Past the fuzzy match's 100 candidates, two works of the name can share the exact title (a
MusicBrainz work and the local work of the same song). ``_create_local_work`` picks the
MusicBrainz one, then the lowest id; the dry run must name the same work.
"""

from __future__ import annotations

from backend.services.grouping_service import planned_work
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.works import FakeWorkRepository

_ARTIST = "Pass The Hundred"


def _repo_with_a_hundred_works() -> tuple[FakeWorkRepository, str]:
    """An artist whose first 100 candidates (by title) are other songs."""
    works = FakeWorkRepository()
    artists = FakeArtistRepository()
    works.set_artist_repo(artists)
    artist_id = artists.upsert_local_artist(_ARTIST, normalize_artist(_ARTIST))
    for n in range(100):
        works.credit(works.create_local(f"Aardvark {n:03d}"), artist_id)
    return works, artist_id


def _credited(works: FakeWorkRepository, work_id: str, artist_id: str) -> str:
    """The work, credited to the artist by a performance of it (a work's artists, 3b S7)."""
    works.credit(work_id, artist_id)
    return work_id


def test_exact_pass_prefers_the_musicbrainz_work() -> None:
    works, artist_id = _repo_with_a_hundred_works()
    local = _credited(works, works.create_local("Zebra Crossing"), artist_id)
    musicbrainz = _credited(
        works, works.upsert_from_mb("00000000-mb-zebra", "Zebra Crossing"), artist_id
    )

    assert planned_work("Zebra Crossing", _ARTIST, works) == musicbrainz != local


def test_exact_pass_takes_the_lowest_id_between_local_works() -> None:
    works, artist_id = _repo_with_a_hundred_works()
    first = _credited(works, works.create_local("Zebra Crossing"), artist_id)
    second = _credited(works, works.create_local("Zebra Crossing"), artist_id)

    assert planned_work("Zebra Crossing", _ARTIST, works) == min(first, second)
