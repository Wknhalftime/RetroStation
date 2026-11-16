"""3a's performance key compares credit artists, not MBIDs only (work merges phase 3b PR 2, SPEC
D1, D17 (scope Q4, Q5); design §6.2; AUD-R078).

A file without a recording MBID joins a performance of its work with the same credit artists,
the same version and a length within 3 s (3a, S2). The credit artists of a file are:

- its tag credit's MBIDs, placeholders (``SPECIAL_PURPOSE_MBIDS``) left out;
- with none left (an untagged file, or only placeholders), its work's artist (D1: that artist is
  every uncredited performance's performer), as its MBID when it has one, else as its artist
  id, so two name-only acts are told apart (Q4);
- with a placeholder work artist, or none, the empty set (Q5, as 3a).

So an untagged copy now has the same credit artists as a tagged one crediting its work's artist,
and joins its performance when version and length agree (§6.2).

D1's credit for a performance whose first file has no credit is one credit, the work's artist:
position 0, its id, its MBID if it has one, its name as the credited name, no joining words,
``main``. A placeholder work artist, or none, gives no credit.
"""

from __future__ import annotations

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.enums import CreditRole, VersionType
from backend.domain.performances import (
    CreditPart,
    NewCredit,
    PerformanceKey,
    WorkArtist,
    credit_artists,
    default_credits,
    fits,
)

BON_JOVI_MBID = "5dcdb5eb-cb72-4e6e-9e63-b7bace604965"
NETTLES_MBID = "0a000000-0000-4000-8000-000000000002"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists

BON_JOVI = WorkArtist(id="artist-bon-jovi", mbid=BON_JOVI_MBID, name="Bon Jovi")
POISON_BAND = WorkArtist(id="artist-poison-band", mbid=None, name="Poison")
POISON_OTHER = WorkArtist(id="artist-poison-other", mbid=None, name="Poison")
PLACEHOLDER = WorkArtist(id="artist-various", mbid=VARIOUS, name="Various Artists")


def key(artists: frozenset[str], length_ms: int = 200_000) -> PerformanceKey:
    return PerformanceKey(
        recording_mbid=None,
        credit_artists=artists,
        version_type=VersionType.ORIGINAL,
        length_ms=length_ms,
    )


def test_the_placeholder_is_one() -> None:
    assert VARIOUS in SPECIAL_PURPOSE_MBIDS


# --- a file's credit artists ----------------------------------------------------------------------


def test_an_mbid_compares_as_itself() -> None:
    """A tag credit is its MBIDs, whatever the work's artist."""
    tagged = (CreditPart(BON_JOVI_MBID, "Bon Jovi", " & "), CreditPart(NETTLES_MBID, "Nettles"))

    assert credit_artists(tagged, POISON_BAND) == {BON_JOVI_MBID, NETTLES_MBID}


def test_an_untagged_file_credits_its_works_artist_by_mbid() -> None:
    assert credit_artists((), BON_JOVI) == {BON_JOVI_MBID}


def test_a_name_only_artist_compares_by_id() -> None:
    assert credit_artists((), POISON_BAND) == {"artist-poison-band"}


def test_placeholders_are_left_out() -> None:
    """A credit of placeholders only is no credit, so the work's artist stands in; a placeholder
    beside a real artist is dropped."""
    only_placeholder = (CreditPart(VARIOUS, "Various Artists"),)
    beside = (CreditPart(VARIOUS, "VA", " / "), CreditPart(NETTLES_MBID, "Nettles"))

    assert credit_artists(only_placeholder, BON_JOVI) == {BON_JOVI_MBID}
    assert credit_artists(beside, BON_JOVI) == {NETTLES_MBID}


def test_a_placeholder_or_missing_work_artist_credits_no_one() -> None:
    assert credit_artists((), PLACEHOLDER) == frozenset()
    assert credit_artists((CreditPart(VARIOUS),), PLACEHOLDER) == frozenset()
    assert credit_artists((), None) == frozenset()


# --- what joins -----------------------------------------------------------------------------------


def test_two_name_only_acts_are_told_apart() -> None:
    """Two untagged "Poison" files of the same length, one by each name-only act, never join:
    the reason a merged work needs D1."""
    band = key(credit_artists((), POISON_BAND))
    other = key(credit_artists((), POISON_OTHER))

    assert not fits(band, other)
    assert fits(band, key(credit_artists((), POISON_BAND), length_ms=202_000))


def test_an_untagged_copy_fits_its_artists_tagged_performance() -> None:
    """§6.2: the same credit artists, version and a close length (Q4)."""
    tagged = key(credit_artists((CreditPart(BON_JOVI_MBID, "Bon Jovi"),), POISON_BAND))
    untagged = key(credit_artists((), BON_JOVI), length_ms=201_000)

    assert fits(untagged, tagged)
    assert not fits(key(credit_artists((), POISON_BAND)), tagged)


# --- D1's credit ----------------------------------------------------------------------------------


def test_the_default_credit_names_the_works_artist() -> None:
    assert default_credits(BON_JOVI) == [
        NewCredit(
            position=0,
            artist_id="artist-bon-jovi",
            mbid=BON_JOVI_MBID,
            credited_name="Bon Jovi",
            join_after=None,
            role=CreditRole.MAIN,
        )
    ]


def test_a_name_only_artist_is_credited_by_id_alone() -> None:
    assert default_credits(POISON_BAND) == [
        NewCredit(
            position=0,
            artist_id="artist-poison-band",
            mbid=None,
            credited_name="Poison",
            join_after=None,
            role=CreditRole.MAIN,
        )
    ]


def test_a_placeholder_or_missing_work_artist_gives_no_credit() -> None:
    assert default_credits(PLACEHOLDER) == []
    assert default_credits(None) == []
