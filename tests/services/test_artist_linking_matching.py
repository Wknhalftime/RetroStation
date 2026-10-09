"""Acceptance tests: what a linked artist changes in matching (AUD-R026; spec 2026-10-05 D15).

Artist matching stays unchanged (Lance, 2026-10-08). The filter is what keeps it so: the fuzzy
pass of NormalizationStrategy skips artists the linker gave an MBID. They still match by exact
name, now with the MBID as target. Without the filter, the tag-only links would let the fuzzy
pass auto-match about 125 undecided broadcast artists on the dev DB, some wrongly
(BEN LEE -> Brenda Lee). An artist whose MBID came from library enrichment stays in the pool.

Song matching reads the catalog artist's MBID when the artist match targets the local id
(identity_matching_service.py:404-415). So after a link, Step A scores the artist's
MBID-tagged files. When Step A auto-matches, Step C (every file under the name, D13) does not
run, so an untagged copy is no longer considered: pinned as today's D13 behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import (
    ArtistLinkOutcome,
    CatalogSource,
    EnrichmentStatus,
    MatchStatus,
    MatchTier,
    TargetType,
)
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services.artist_matching_service import NormalizationStrategy
from backend.services.identity_matching_service import (
    IdentityMatchResult,
    ResolvedArtistMbidStrategy,
)
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
QUEEN = "00000000-0000-4000-8000-000000000001"
CASE = "00000000-0000-4000-8000-000000000002"
BOZ = "00000000-0000-4000-8000-000000000003"
SOUNDGARDEN = "00000000-0000-4000-8000-000000000004"


def _catalog(
    name: str,
    mbid: str | None,
    *,
    origin: CatalogSource = CatalogSource.MUSICBRAINZ,
    outcome: ArtistLinkOutcome | None = None,
) -> Artist:
    return Artist(
        id=str(uuid4()),
        name=name,
        sort_name=name,
        normalized_name=normalize_artist(name),
        mbid=mbid,
        origin=origin,
        needs_enhancement=False,
        mb_lookup_at=T0 if outcome is not None else None,
        mb_lookup_outcome=outcome,
    )


def _broadcast(name: str, status: MatchStatus = MatchStatus.PENDING) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(),
        original_name=name,
        normalized_name=normalize_artist(name),
        match_status=status,
    )


# --- Artist matching -------------------------------------------------------------------------


def test_linked_by_lookup_is_true_only_for_the_linkers_links() -> None:
    assert _catalog("Case", CASE, outcome=ArtistLinkOutcome.LINKED).linked_by_lookup is True
    assert _catalog("Case", CASE).linked_by_lookup is False
    assert _catalog("Case", CASE, outcome=ArtistLinkOutcome.TAG_MISMATCH).linked_by_lookup is False


def test_an_artist_with_an_mbid_from_enrichment_is_in_the_fuzzy_pass() -> None:
    # Today's behaviour, kept: 88.9 with a 68.9-point lead over Queen auto-matches.
    strategy = NormalizationStrategy([_catalog("Queen", QUEEN), _catalog("Case", CASE)])

    result = strategy.apply(_broadcast("CHASE"))

    assert result is not None
    assert (result.status, result.target_id) == (MatchStatus.AUTO_MATCHED, CASE)


def test_a_linked_artist_does_not_join_the_fuzzy_pass() -> None:
    linked_case = _catalog("Case", CASE, outcome=ArtistLinkOutcome.LINKED)
    strategy = NormalizationStrategy([_catalog("Queen", QUEEN), linked_case])

    # Queen alone scores 20 against "chase", under the presentation floor: no result, the same
    # as before the link, when Case was a local artist without an MBID.
    assert strategy.apply(_broadcast("CHASE")) is None


def test_an_artist_enrichment_promoted_after_a_lookup_is_in_the_fuzzy_pass() -> None:
    promoted = _catalog("Case", CASE, outcome=ArtistLinkOutcome.TAG_MISMATCH)
    strategy = NormalizationStrategy([_catalog("Queen", QUEEN), promoted])

    result = strategy.apply(_broadcast("CHASE"))

    assert result is not None
    assert result.target_id == CASE


def test_a_linked_artist_still_matches_by_exact_name() -> None:
    linked = _catalog("Boz Scaggs", BOZ, outcome=ArtistLinkOutcome.LINKED)

    result = NormalizationStrategy([linked]).apply(_broadcast("BOZ SCAGGS"))

    assert result is not None
    assert (result.status, result.tier, result.confidence_score, result.target_id) == (
        MatchStatus.AUTO_MATCHED,
        MatchTier.NORMALIZATION,
        100.0,
        BOZ,
    )


# --- Song matching ---------------------------------------------------------------------------


def _file(title: str, tag: str | None) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        audio=AudioMetadata(
            track_title=title,
            normalized_title=normalize_title(title),
            artist_name="Soundgarden",
            normalized_artist_name="soundgarden",
            artist_mbid=tag,
        ),
    )


def _song_match(
    *, linked: bool, files: list[LibraryFile]
) -> tuple[IdentityMatchResult | None, FakeMbClient]:
    """One auto-matched broadcast artist whose match row targets the LOCAL catalog id."""
    catalog = FakeArtistRepository()
    local = catalog.upsert(
        _catalog(
            "Soundgarden",
            SOUNDGARDEN if linked else None,
            origin=CatalogSource.MUSICBRAINZ if linked else CatalogSource.LOCAL,
            outcome=ArtistLinkOutcome.LINKED if linked else None,
        )
    )
    artist = _broadcast("SOUNDGARDEN", MatchStatus.AUTO_MATCHED)
    matches = FakeMatchRepository()
    matches.create(
        Match(
            id=uuid4(),
            artist_id=artist.id,
            target_id=local.id,
            target_type=TargetType.ARTIST,
            confidence_score=100.0,
            match_tier=MatchTier.NORMALIZATION,
        )
    )
    library = FakeLibraryFileRepository()
    for library_file in files:
        library.upsert(library_file)
    norm_title = normalize_title("Black Hole Sun")
    song = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title="Black Hole Sun",
        normalized_title=norm_title,
        normalized_signature=compute_normalized_signature("soundgarden", norm_title),
    )
    mb = FakeMbClient()
    strategy = ResolvedArtistMbidStrategy(
        library_file_repo=library, match_repo=matches, mb_client=mb, catalog_repo=catalog
    )
    return strategy.apply(song, artist), mb


def test_before_a_link_the_song_matches_by_name() -> None:
    tagged = _file("Black Hole Sun", SOUNDGARDEN)

    result, mb = _song_match(linked=False, files=[tagged])

    assert result is not None
    assert (result.status, result.tier, result.library_file_id) == (
        MatchStatus.AUTO_MATCHED,
        MatchTier.LOCAL_FILE_FUZZY,
        tagged.id,
    )
    assert mb.calls == []


def test_after_a_link_step_a_finds_the_artists_tagged_file() -> None:
    tagged = _file("Black Hole Sun", SOUNDGARDEN)

    result, mb = _song_match(linked=True, files=[tagged])

    assert result is not None
    assert (result.status, result.tier, result.library_file_id) == (
        MatchStatus.AUTO_MATCHED,
        MatchTier.MUSICBRAINZ_ID_EXACT,
        tagged.id,
    )
    assert mb.calls == []


def test_after_a_link_a_step_a_auto_match_pre_empts_an_untagged_copy() -> None:
    # D13 consequence, now reachable for linked artists (review S4): Step A auto-matches the
    # tagged live take on its own, so Step C never scores the untagged studio take.
    live = _file("Black Hole Sun (Live)", SOUNDGARDEN)
    studio = _file("Black Hole Sun", None)

    before, _ = _song_match(linked=False, files=[live, studio])
    after, _ = _song_match(linked=True, files=[live, studio])

    assert before is not None
    assert after is not None
    assert (before.library_file_id, before.tier) == (studio.id, MatchTier.LOCAL_FILE_FUZZY)
    assert (after.library_file_id, after.tier, after.status) == (
        live.id,
        MatchTier.MUSICBRAINZ_ID_EXACT,
        MatchStatus.AUTO_MATCHED,
    )
