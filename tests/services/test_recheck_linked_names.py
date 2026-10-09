"""Acceptance tests: newly linked artists join the targeted re-check's wave (AUD-R026; D15).

Lance, 2026-10-08: "Newly linked artists' names join the next targeted re-check wave
automatically, so their undecided songs are re-scored." recheck_names takes a second source of
names: ArtistLinkingRepository.normalized_names_linked_since(watermark). So the "changed" wave is
the artists of files indexed or gone missing after the watermark, plus every artist linked
after it, including one linked by a later run after a transient failure. Scope "all" and the
first run (no watermark) still mean everything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from backend.domain.catalog import Artist
from backend.domain.enums import ArtistLinkOutcome, CatalogSource, EnrichmentStatus, RecheckScope
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.matching_recheck_service import recheck_names
from backend.services.normalization import normalize_artist
from tests.fakes.artist_linking import FakeArtistLinkingRepository
from tests.fakes.library_files import FakeLibraryFileRepository

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _artist(
    name: str, *, looked_up_at: datetime, outcome: ArtistLinkOutcome = ArtistLinkOutcome.LINKED
) -> Artist:
    linked = outcome == ArtistLinkOutcome.LINKED
    return Artist(
        id=str(uuid4()),
        name=name,
        sort_name=name,
        normalized_name=normalize_artist(name),
        mbid=f"00000000-0000-4000-8000-{uuid4().int % 10**12:012d}" if linked else None,
        origin=CatalogSource.MUSICBRAINZ if linked else CatalogSource.LOCAL,
        needs_enhancement=False,
        mb_lookup_at=looked_up_at,
        mb_lookup_outcome=outcome,
    )


def _changed_file(name: str, indexed_at: datetime) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        indexed_at=indexed_at,
        audio=AudioMetadata(
            track_title="Song", artist_name=name, normalized_artist_name=normalize_artist(name)
        ),
    )


def test_an_artist_linked_after_the_watermark_joins_the_wave() -> None:
    files = FakeLibraryFileRepository()
    linking = FakeArtistLinkingRepository()
    linking.add(_artist("Nirvana", looked_up_at=T0 + HOUR), broadcast_names=("nirvana uk",))

    names = recheck_names(RecheckScope.CHANGED, T0, files, linking)

    assert names == {"nirvana", "nirvana uk"}
    assert linking.linked_names_asked == [T0]


def test_the_wave_keeps_the_changed_files_names_beside_the_linked_ones() -> None:
    files = FakeLibraryFileRepository()
    files.upsert(_changed_file("ABBA", T0 + HOUR))
    linking = FakeArtistLinkingRepository()
    linking.add(_artist("Nirvana", looked_up_at=T0 + HOUR))

    assert recheck_names(RecheckScope.CHANGED, T0, files, linking) == {"abba", "nirvana"}


def test_an_artist_linked_before_the_watermark_does_not_join_the_wave() -> None:
    linking = FakeArtistLinkingRepository()
    linking.add(_artist("Nirvana", looked_up_at=T0 - HOUR))

    assert recheck_names(RecheckScope.CHANGED, T0, FakeLibraryFileRepository(), linking) == set()


def test_an_artist_linked_by_a_later_run_joins_that_runs_wave() -> None:
    # The backlog's run failed on Nirvana (a 503); the next MB pass's linking run, after the
    # re-check that followed the backlog (watermark T0 + 1h), linked it at T0 + 2h.
    linking = FakeArtistLinkingRepository()
    linking.add(_artist("Nirvana", looked_up_at=T0 + 2 * HOUR))

    names = recheck_names(RecheckScope.CHANGED, T0 + HOUR, FakeLibraryFileRepository(), linking)

    assert names == {"nirvana"}


def test_artists_stamped_without_a_link_do_not_join_the_wave() -> None:
    linking = FakeArtistLinkingRepository()
    for outcome in ArtistLinkOutcome:
        if outcome != ArtistLinkOutcome.LINKED:
            linking.add(_artist(f"Band {outcome.value}", looked_up_at=T0 + HOUR, outcome=outcome))

    assert recheck_names(RecheckScope.CHANGED, T0, FakeLibraryFileRepository(), linking) == set()


def test_scope_all_and_the_first_run_still_mean_everything() -> None:
    linking = FakeArtistLinkingRepository()
    linking.add(_artist("Nirvana", looked_up_at=T0 + HOUR))

    assert recheck_names(RecheckScope.ALL, T0, FakeLibraryFileRepository(), linking) is None
    assert recheck_names(RecheckScope.CHANGED, None, FakeLibraryFileRepository(), linking) is None
    assert linking.linked_names_asked == []
