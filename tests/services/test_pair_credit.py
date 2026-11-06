"""Pairing a file's artist names with its MBIDs (phase 2a S4, E2; C69, M1, M32; AUD-R041).

The shapes C69 measured: a single MBID, MBIDs and names by position, MBIDs joined in one value,
counts that differ, no ``artists`` tag, a credited-as name that isn't in the printed credit, and
joining words read from the printed ``artist``. MBIDs split on the MBID pattern and names are
never split. Where the tags can't name an MBID, the names come from MusicBrainz, and where
MusicBrainz now credits someone else the tags stand.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.library import CreditEntry
from backend.services.artist_credit import (
    credit_from_raw_metadata,
    name_from_musicbrainz,
    pair_credit,
    split_mbids,
    tag_values,
)

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
THREE = "0a000000-0000-4000-8000-000000000003"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]


# --- splitting ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        [ONE, TWO],
        [f"{ONE}, {TWO}"],
        [f"{ONE}; {TWO}"],
        [f"{ONE}/{TWO}"],
        [f"{ONE} {TWO}"],
        [f"{ONE}{TWO}"],
        [f"{ONE}\x00{TWO}"],
        [ONE.upper(), TWO.upper()],
        ["  " + ONE + "  ", TWO],
    ],
    ids=[
        "two values",
        "comma",
        "semicolon",
        "slash",
        "space",
        "no separator, as an ID3 frame keeps them",
        "a NUL",
        "upper case",
        "padded",
    ],
)
def test_mbids_split_on_the_pattern(values: list[str]) -> None:
    assert split_mbids(values) == (ONE, TWO)


def test_what_is_not_an_mbid_is_left_out() -> None:
    assert split_mbids(["not-an-mbid", "", "1234", f"junk {ONE} junk"]) == (ONE,)
    assert split_mbids([]) == ()


def test_a_placeholder_stays_in_the_files_mbids() -> None:
    """C7: it gets no artist, which is the sweep's to refuse. The file still holds the value."""
    assert UNKNOWN in SPECIAL_PURPOSE_MBIDS
    assert split_mbids([ONE, UNKNOWN]) == (ONE, UNKNOWN)


def test_duplicate_mbid() -> None:
    """E2: a tag listing one MBID twice keeps it once, at its first position."""
    assert split_mbids([TWO, ONE, TWO, TWO.upper()]) == (TWO, ONE)


# --- pairing --------------------------------------------------------------------------------------


def test_one_mbid_pairs_with_the_whole_artist_text() -> None:
    """Nothing to locate and no lookup: the printed text is the name, joiners and all (M32)."""
    assert pair_credit("Hall & Oates", [], [ONE]) == (CreditEntry(ONE, "Hall & Oates", ""),)


def test_one_mbid_ignores_the_artists_tag() -> None:
    assert pair_credit("Hall & Oates", ["Daryl Hall"], [ONE]) == (
        CreditEntry(ONE, "Hall & Oates", ""),
    )


def test_one_mbid_with_no_artist_text_has_no_name() -> None:
    assert pair_credit(None, [], [ONE]) == (CreditEntry(ONE, None, None),)


def test_no_mbid_is_no_credit() -> None:
    assert pair_credit("Hall & Oates", ["Hall & Oates"], []) is None


@pytest.mark.parametrize(
    ("artist", "names", "joins"),
    [
        ("Santana feat. Rob Thomas", ["Santana", "Rob Thomas"], [" feat. ", ""]),
        ("Queen & David Bowie", ["Queen", "David Bowie"], [" & ", ""]),
        ("Simon, Garfunkel", ["Simon", "Garfunkel"], [", ", ""]),
    ],
)
def test_names_pair_by_position_and_joins_come_from_the_print(
    artist: str, names: list[str], joins: list[str]
) -> None:
    assert pair_credit(artist, names, [ONE, TWO]) == (
        CreditEntry(ONE, names[0], joins[0]),
        CreditEntry(TWO, names[1], joins[1]),
    )


def test_three_names_in_order() -> None:
    assert pair_credit("A, B & C", ["A", "B", "C"], [ONE, TWO, THREE]) == (
        CreditEntry(ONE, "A", ", "),
        CreditEntry(TWO, "B", " & "),
        CreditEntry(THREE, "C", ""),
    )


def test_text_after_the_last_name_is_its_join() -> None:
    assert pair_credit("A & B and friends", ["A", "B"], [ONE, TWO]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(TWO, "B", " and friends"),
    )


def test_mbids_joined_in_one_value_still_pair_by_position() -> None:
    assert pair_credit("A & B", ["A", "B"], [f"{ONE}, {TWO}"]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(TWO, "B", ""),
    )


def test_a_name_is_never_split() -> None:
    """M32: the ``artists`` tag's values are the names, whatever they hold."""
    assert pair_credit("Hall & Oates feat. B", ["Hall & Oates", "B"], [ONE, TWO]) == (
        CreditEntry(ONE, "Hall & Oates", " feat. "),
        CreditEntry(TWO, "B", ""),
    )


def test_a_credited_as_name_keeps_its_pair_with_no_join() -> None:
    """C69: "Strait Up" prints "Snot". The name stays with its MBID by position, and the joins
    touching it are unknown. The other side's join can still be read."""
    assert pair_credit("Snot & Lajon", ["Strait Up", "Lajon"], [ONE, TWO]) == (
        CreditEntry(ONE, "Strait Up", None),
        CreditEntry(TWO, "Lajon", ""),
    )
    assert pair_credit("A & Lajon", ["A", "Strait Up"], [ONE, TWO]) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "Strait Up", None),
    )


def test_names_are_located_in_order() -> None:
    """A name found only before the one ahead of it isn't where its position says."""
    assert pair_credit("B & A", ["A", "B"], [ONE, TWO]) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "B", None),
    )


def test_a_name_that_repeats_is_found_after_the_one_before_it() -> None:
    assert pair_credit("A vs. A", ["A", "A"], [ONE, TWO]) == (
        CreditEntry(ONE, "A", " vs. "),
        CreditEntry(TWO, "A", ""),
    )


def test_no_printed_artist_leaves_every_join_unknown() -> None:
    assert pair_credit(None, ["A", "B"], [ONE, TWO]) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "B", None),
    )


@pytest.mark.parametrize(
    "names",
    [[], ["A"], ["A", "B", "C"]],
    ids=["no artists tag", "fewer names", "more names"],
)
def test_counts_that_differ_leave_the_names_null(names: list[str]) -> None:
    """Position can't be trusted, and the print is never split on names: the names wait for
    MusicBrainz, and the scan's do for enrichment."""
    assert pair_credit("A & B", names, [ONE, TWO]) == (
        CreditEntry(ONE, None, None),
        CreditEntry(TWO, None, None),
    )


def test_a_duplicate_keeps_its_first_name() -> None:
    """E2 with both tags doubled: the counts match, so position decides, then the repeat goes.
    Its first name stays, whatever the repeat calls it."""
    assert pair_credit("A & B", ["A", "B", "Ayy"], [ONE, TWO, ONE]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(TWO, "B", ""),
    )


def test_a_duplicate_mbid_alone_still_pairs() -> None:
    """E2 with only the MBID tag doubled: kept once, the counts match."""
    assert pair_credit("A & B", ["A", "B"], [ONE, TWO, ONE]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(TWO, "B", ""),
    )


def test_a_duplicate_that_collapses_to_one_is_a_single_mbid() -> None:
    assert pair_credit("Hall & Oates", [], [ONE, ONE]) == (CreditEntry(ONE, "Hall & Oates", ""),)


def test_a_placeholder_pairs_like_any_other() -> None:
    assert pair_credit("A & [unknown]", ["A", "[unknown]"], [ONE, UNKNOWN]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(UNKNOWN, "[unknown]", ""),
    )


# --- saved tag data -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "values"),
    [
        ("['a', 'b']", ["a", "b"]),
        ("['a']", ["a"]),
        ('["it\'s", "b"]', ["it's", "b"]),
        ("plain text", ["plain text"]),
        ("[not a list", ["[not a list"]),
        (["a", "b"], ["a", "b"]),
        ("", []),
        (None, []),
    ],
    ids=[
        "a Vorbis list",
        "one value",
        "a quote in a value",
        "plain text",
        "a bracket that is no list",
        "a stored list",
        "empty",
        "absent",
    ],
)
def test_tag_values_read_what_raw_metadata_kept(raw: object, values: list[str]) -> None:
    """``raw_metadata`` stores ``str(value)``, so a Vorbis list arrives as its printed form."""
    assert tag_values(raw) == values


def test_a_vorbis_row_gives_its_columns() -> None:
    raw = {
        "artist": "['A & B']",
        "artists": "['A', 'B']",
        "musicbrainz_artistid": f"['{ONE}', '{TWO}']",
    }

    assert credit_from_raw_metadata(raw) == (
        (ONE, TWO),
        (CreditEntry(ONE, "A", " & "), CreditEntry(TWO, "B", "")),
    )


def test_a_joined_vorbis_value_gives_its_columns() -> None:
    raw = {"artist": "['A & B']", "musicbrainz_artistid": f"['{ONE}, {TWO}']"}

    assert credit_from_raw_metadata(raw) == (
        (ONE, TWO),
        (CreditEntry(ONE, None, None), CreditEntry(TWO, None, None)),
    )


def test_a_row_with_no_mbid_tag_is_untagged() -> None:
    assert credit_from_raw_metadata({"artist": "['A']", "title": "['Song']"}) == ((), None)
    assert credit_from_raw_metadata({}) == ((), None)


# --- names from MusicBrainz -----------------------------------------------------------------------


def _unnamed(*mbids: str) -> tuple[CreditEntry, ...]:
    return tuple(CreditEntry(mbid) for mbid in mbids)


def _credit(mbid: str, name: str, join: str | None = None) -> dict[str, Any]:
    # A real credit's own name can be a credited-as one: the artist's is what names the MBID.
    credit: dict[str, Any] = {"name": f"credited as {name}", "artist": {"id": mbid, "name": name}}
    if join is not None:
        credit["joinphrase"] = join
    return credit


def test_cached_artists_name_their_mbids() -> None:
    assert name_from_musicbrainz(_unnamed(ONE, TWO), {ONE: "A", TWO: "B"}, None) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "B", None),
    )


def test_a_credit_names_what_the_artists_did_not() -> None:
    credit = [_credit(ONE, "Other Spelling"), _credit(TWO, "B")]

    assert name_from_musicbrainz(_unnamed(ONE, TWO), {ONE: "A"}, credit) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "B", None),
    )


def test_a_full_credit_gives_its_joining_words() -> None:
    """C69: when the print can't, the words come from the MusicBrainz credit."""
    credit = [_credit(ONE, "A", " feat. "), _credit(TWO, "B", "")]

    assert name_from_musicbrainz(_unnamed(ONE, TWO), {}, credit) == (
        CreditEntry(ONE, "A", " feat. "),
        CreditEntry(TWO, "B", ""),
    )


def test_a_slim_credit_has_no_joining_words() -> None:
    """S4: a slim ``recording-by-mbid`` row keeps none, so a join only it could give stays null."""
    credit = [_credit(ONE, "A"), _credit(TWO, "B")]

    assert name_from_musicbrainz(_unnamed(ONE, TWO), {}, credit) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "B", None),
    )


def test_when_musicbrainz_credits_someone_else_the_tags_stand() -> None:
    """M1: only the file's own MBIDs are named. A credited stranger adds nothing, and an MBID
    MusicBrainz no longer credits stays unnamed, for its stub."""
    credit = [_credit(ONE, "A", " & "), _credit(THREE, "Stranger", "")]

    assert name_from_musicbrainz(_unnamed(ONE, TWO), {}, credit) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, None, None),
    )


def test_the_tags_keep_their_order() -> None:
    credit = [_credit(TWO, "B", " & "), _credit(ONE, "A", "")]

    assert name_from_musicbrainz(_unnamed(ONE, TWO), {}, credit) == (
        CreditEntry(ONE, "A", None),
        CreditEntry(TWO, "B", None),
    )


def test_a_name_the_tags_gave_stands() -> None:
    entries = (CreditEntry(ONE, "From The Tag", " & "), CreditEntry(TWO))

    assert name_from_musicbrainz(
        entries, {ONE: "From The Cache", TWO: "B"}, [_credit(ONE, "From The Credit")]
    ) == (
        CreditEntry(ONE, "From The Tag", " & "),
        CreditEntry(TWO, "B", None),
    )


def test_nothing_known_names_nothing() -> None:
    assert name_from_musicbrainz(_unnamed(ONE), {}, None) == (CreditEntry(ONE, None, None),)
    assert name_from_musicbrainz(_unnamed(ONE), {}, []) == (CreditEntry(ONE, None, None),)


def test_a_credit_with_no_artist_is_passed_by() -> None:
    credit: list[dict[str, Any]] = [{"name": "A"}, {"artist": {}}, _credit(ONE, "A")]

    assert name_from_musicbrainz(_unnamed(ONE), {}, credit) == (CreditEntry(ONE, "A", None),)


# --- what real tags hold ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        ["Earth, Wind & Fire", "B"],
        ["it's", 'say "hi"'],
        ["it's", "plain"],
        ["back\\slash", "B"],
        ["[bracketed]", "B"],
        ["one, two, three"],
    ],
    ids=["a comma", "both quotes", "one quote", "a backslash", "brackets", "one value of commas"],
)
def test_a_saved_list_keeps_each_value_whole(values: list[str]) -> None:
    """M32: its printed form, as ``str()`` wrote it. Splitting that text on commas would split a
    name."""
    assert tag_values(str(values)) == values


def test_a_nul_in_a_saved_value_is_dropped() -> None:
    """``str(list)`` escapes it past the scan's sanitising, and JSONB refuses it."""
    assert tag_values(str(["A\x00B"])) == ["AB"]


def test_a_malformed_value_keeps_its_place() -> None:
    """Three values and two names can't pair, whatever the bad one is. Dropping it first would
    shift every name onto the wrong MBID."""
    assert pair_credit("X & A", ["X", "A"], ["bad", ONE, TWO]) == (
        CreditEntry(ONE),
        CreditEntry(TWO),
    )


def test_a_malformed_value_pairs_with_its_own_name() -> None:
    assert pair_credit("X & A & B", ["X", "A", "B"], ["bad", ONE, TWO]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(TWO, "B", ""),
    )
    assert pair_credit("A & B", ["A", "B"], [ONE, "bad"]) == (CreditEntry(ONE, "A", " & B"),)


def test_one_mbid_beside_a_malformed_value_takes_no_print() -> None:
    """It isn't a single-MBID file: the print may name the other value's artist too."""
    assert pair_credit("A & B", [], [ONE, "bad"]) == (CreditEntry(ONE),)


def test_text_is_cleaned_as_the_reader_cleans_it() -> None:
    assert pair_credit(" A ", [], [ONE]) == (CreditEntry(ONE, "A", ""),)
    assert pair_credit("", [], [ONE]) == (CreditEntry(ONE, None, None),)
    assert pair_credit("   ", [], [ONE]) == (CreditEntry(ONE, None, None),)
    assert pair_credit("A & B", [" A ", "B\x00"], [ONE, TWO]) == (
        CreditEntry(ONE, "A", " & "),
        CreditEntry(TWO, "B", ""),
    )


def test_a_blank_name_is_no_name() -> None:
    """Named by MusicBrainz later, as an absent one is."""
    assert pair_credit("A & B", ["", "B"], [ONE, TWO]) == (
        CreditEntry(ONE, None, None),
        CreditEntry(TWO, "B", ""),
    )
