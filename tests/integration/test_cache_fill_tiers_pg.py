"""What each tier of the cache fill searches, and in what order (MusicBrainz cache fill, phase 4.1
PR 2; SPEC S3; D5; the coordinator's readings for PR 2's search words; design C2, C8, C18, C67,
C68, M25; AUD-R029, AUD-R091), on PostgreSQL.

Read through the calls one run makes with the default limits; every seed here fits one batch.

- **payoff:** (name, normalized title) pairs of the names searched (in review or auto rejected,
  and not on the not-an-artist list), whose title a present library file holds and is not
  common, most plays first. A pair's plays are all its lines' plays; the title sent is its
  most-played line's original title.
- **C68:** present untagged files whose artist text has a joiner and is no known act with an
  MBID (by an artist's normalized name or one of its ``artist_names``), one search per words and
  title, most files first.
- **C18:** names whose one active link is manual, to an artist holding an MBID, with a joiner in
  the name: one search under that MBID per title, by plays, sending the title song matching sends
  (``broadcast_title_variants``' first form), so the cache row is the one song matching reads.
- **confirmations:** 0058's queued pairs, oldest first, whatever the name's status, except a
  manually rejected or not-an-artist name.
- **tail:** payoff's common-title pairs first, then pairs whose title no present file holds, by
  plays.

C8: a title is common when 3 or more distinct acts hold it. On the library side, each present
file's credited artists count by MBID, and an untagged file by its normalized artist name. On the
log side, each log name that played the title counts through its active links, by the catalog
artist each link points at (by its MBID when it holds one), so two spellings of one act count
once, an unlinked name counts for nothing, and an act on both sides counts once.

Two pairs with one cache key make one call.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration.cache_fill_rig import (
    Asked,
    Conn,
    Fill,
    cache_keys,
    cached,
    catalog_artist,
    confirmation,
    key,
    known_name,
    library_file,
    line,
    link,
    log_name,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 22, 9, tzinfo=UTC)


def mbid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def fill(conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fill]:
    rig = Fill(conn, migrated_db, monkeypatch)
    yield rig
    rig.close()


def searched(fill: Fill) -> list[Asked]:
    """The name + title searches made, in order."""
    return fill.server.asked("name_title")


# --- payoff and tail ----------------------------------------------------------------------------


def test_payoff_is_titles_in_the_library_and_the_tail_the_rest(conn: Conn, fill: Fill) -> None:
    heart = log_name(conn, "HEART")
    styx = log_name(conn, "STYX", "auto_rejected")
    line(conn, heart, "Barracuda", plays=5)
    line(conn, heart, "Magic Man", plays=50)  # no file holds it
    line(conn, styx, "Renegade", plays=20)
    line(conn, styx, "Come Sail Away", plays=40)  # its only file is missing
    library_file(conn, "Barracuda", mbids=[mbid(1)])
    library_file(conn, "Renegade", mbids=[mbid(2)])
    library_file(conn, "Come Sail Away", mbids=[mbid(3)], status="missing")

    fill.run()

    assert searched(fill) == [
        Asked("name_title", ("STYX",), "Renegade"),  # payoff, by plays
        Asked("name_title", ("HEART",), "Barracuda"),
        Asked("name_title", ("HEART",), "Magic Man"),  # tail, by plays
        Asked("name_title", ("STYX",), "Come Sail Away"),
    ]


def test_a_pairs_plays_are_all_its_lines_and_its_title_the_most_played(
    conn: Conn, fill: Fill
) -> None:
    queen = log_name(conn, "QUEEN & DAVID BOWIE")
    line(conn, queen, "Under Pressure", plays=5)
    line(conn, queen, "UNDER PRESSURE", plays=9)  # the same pair: 14 plays
    bowie = log_name(conn, "DAVID BOWIE & QUEEN")
    line(conn, bowie, "Under Pressure", plays=12)
    library_file(conn, "Under Pressure", mbids=[mbid(1), mbid(2)])

    fill.run()

    assert searched(fill) == [
        Asked("name_title", ("QUEEN", "DAVID", "BOWIE"), "UNDER PRESSURE"),
        Asked("name_title", ("DAVID", "BOWIE", "QUEEN"), "Under Pressure"),
    ]


def test_c8_common_titles_go_to_the_front_of_the_tail(conn: Conn, fill: Fill) -> None:
    newcomer = log_name(conn, "NEWCOMER")
    x = catalog_artist(conn, "Act X", mbid(10))
    y = catalog_artist(conn, "Act Y", mbid(11))
    z = catalog_artist(conn, "Act Z", mbid(12))
    one = catalog_artist(conn, "Act One", mbid(1))  # the act files credit as mbid(1)
    local = catalog_artist(conn, "Local Act")  # no MBID

    def heard(title: str, plays: int) -> None:
        line(conn, newcomer, title, plays=plays)

    def played_by(original: str, artist_id: str, title: str, state: str = "active") -> None:
        name = log_name(conn, original, "auto_matched" if state == "active" else "manual_rejected")
        link(conn, name, artist_id, state=state)
        line(conn, name, title, plays=1, status="auto_matched")

    def files(title: str, *credits: list[str]) -> None:
        """One present file per credit, crediting its MBIDs."""
        for credit in credits:
            library_file(conn, title, mbids=credit)

    # Common: 3 acts or more.
    heard("Three Files", 90)
    files("Three Files", [mbid(1)], [mbid(2)], [mbid(3)])
    heard("Two Files And A Log Act", 80)
    files("Two Files And A Log Act", [mbid(1)], [mbid(2)])
    played_by("LINKED TO X", x, "Two Files And A Log Act")
    heard("A Credit And A File", 40)
    files("A Credit And A File", [mbid(1), mbid(2)], [mbid(3)])
    heard("Two Untagged And A File", 30)
    library_file(conn, "Two Untagged And A File", artist="Xavier")
    library_file(conn, "Two Untagged And A File", artist="Yolanda")
    files("Two Untagged And A File", [mbid(1)])
    heard("A Local Act", 20)
    files("A Local Act", [mbid(1)], [mbid(2)])
    played_by("LINKED TO LOCAL", local, "A Local Act")
    # Not common: 2 acts.
    heard("Same Act Both Sides", 70)
    files("Same Act Both Sides", [mbid(1)], [mbid(2)])
    played_by("LINKED TO ONE", one, "Same Act Both Sides")
    heard("Two Spellings One Act", 60)
    files("Two Spellings One Act", [mbid(1)])
    played_by("Y SPELLED ONE WAY", y, "Two Spellings One Act")
    played_by("Y SPELLED ANOTHER", y, "Two Spellings One Act")
    heard("A Link Not Active", 50)
    files("A Link Not Active", [mbid(1)], [mbid(2)])
    played_by("REJECTED Z", z, "A Link Not Active", state="rejected")
    heard("One Credit Twice", 35)
    files("One Credit Twice", [mbid(1), mbid(2)], [mbid(1), mbid(2)])
    heard("One Untagged Act Twice", 25)
    library_file(conn, "One Untagged Act Twice", artist="Xavier")
    library_file(conn, "One Untagged Act Twice", artist="Xavier")
    files("One Untagged Act Twice", [mbid(1)])
    heard("A Missing File", 15)
    files("A Missing File", [mbid(1)], [mbid(2)])
    library_file(conn, "A Missing File", mbids=[mbid(3)], status="missing")
    # Held by no file.
    heard("Not Held", 10)

    fill.run()

    assert [a.title for a in searched(fill)] == [
        # payoff
        "Same Act Both Sides",
        "Two Spellings One Act",
        "A Link Not Active",
        "One Credit Twice",
        "One Untagged Act Twice",
        "A Missing File",
        # tail: the common titles first, then what the library doesn't hold
        "Three Files",
        "Two Files And A Log Act",
        "A Credit And A File",
        "Two Untagged And A File",
        "A Local Act",
        "Not Held",
    ]
    assert {a.words for a in searched(fill)} == {("NEWCOMER",)}


def test_two_names_with_one_key_make_one_call(conn: Conn, fill: Fill) -> None:
    """M9: "HALL AND OATES" and "HALL FT OATES" both send HALL and OATES: one call, and the
    payoff re-check names both."""
    one = log_name(conn, "HALL AND OATES")
    two = log_name(conn, "HALL FT OATES")
    line(conn, one, "Rich Girl", plays=3)
    line(conn, two, "Rich Girl", plays=2)
    library_file(conn, "Rich Girl", mbids=[mbid(1)])

    fill.run()

    assert searched(fill) == [Asked("name_title", ("HALL", "OATES"), "Rich Girl")]
    assert fill.rechecks() == [sorted([one.normalized, two.normalized])]


# --- the words and the title sent -----------------------------------------------------------------


def test_a_cut_off_name_and_cut_titles(conn: Conn, fill: Fill) -> None:
    """Readings 1 and 4: a name the stage reads as cut off (30-character field, ending mid-word)
    drops its last word; a "..." title drops the mark and the word it cuts, and a title that is
    only a cut word is not searched."""
    flaky = log_name(conn, "FLAKY BAND THAT ALMOST RESOLV")  # 29 characters
    line(conn, flaky, "I'LL BE HOME FOR CHRIST...", plays=3)
    line(conn, flaky, "SOMETHI...", plays=2)
    short = log_name(conn, "THE ALLMAN BR")  # short: not read as cut off
    line(conn, short, "JESSICA", plays=1)

    fill.run()

    assert searched(fill) == [
        Asked("name_title", ("FLAKY", "BAND", "THAT", "ALMOST"), "I'LL BE HOME FOR"),
        Asked("name_title", ("ALLMAN", "BR"), "JESSICA"),
    ]
    assert key("FLAKY BAND THAT ALMOST", "I'LL BE HOME FOR") in cache_keys(conn)


# --- C68 -----------------------------------------------------------------------------------------


def test_c68_searches_untagged_files_with_a_joiner(conn: Conn, fill: Fill) -> None:
    for _ in range(3):
        library_file(conn, "Suite: Judy Blue Eyes", artist="Crosby, Stills & Nash")
    for _ in range(2):
        library_file(conn, "The Boxer", artist="Simon & Garfunkel")
    library_file(conn, "Mrs. Robinson", artist="Simon & Garfunkel")
    library_file(conn, "Danny's Song", artist="Loggins and Messina")
    catalog_artist(conn, "Loggins and Messina")  # known, but holds no MBID: still searched
    # Not searched:
    library_file(conn, "Rich Girl", artist="Hall and Oates")
    catalog_artist(conn, "Hall and Oates", mbid(20))  # a known act with an MBID
    library_file(conn, "Neon Moon", artist="Kix Brooks with Ronnie Dunn")
    known_name(conn, catalog_artist(conn, "Brooks & Dunn", mbid(21)), "Kix Brooks with Ronnie Dunn")
    library_file(conn, "Under Pressure", artist="Queen & David Bowie", mbids=[mbid(1), mbid(2)])
    library_file(conn, "Purple Rain", artist="Prince")  # no joiner
    library_file(conn, "Mother Earth", artist="Within Temptation")  # "with" inside a word
    library_file(conn, "Ohio", artist="Crosby, Stills, Nash & Young", status="missing")

    fill.run()

    calls = searched(fill)
    assert calls[:2] == [
        Asked("name_title", ("Crosby", "Stills", "Nash"), "Suite: Judy Blue Eyes"),
        Asked("name_title", ("Simon", "Garfunkel"), "The Boxer"),
    ]
    assert set(calls[2:]) == {
        Asked("name_title", ("Simon", "Garfunkel"), "Mrs. Robinson"),
        Asked("name_title", ("Loggins", "Messina"), "Danny's Song"),
    }
    assert len(calls) == 4


# --- C18 -----------------------------------------------------------------------------------------


def test_c18_searches_a_manual_names_titles_under_its_one_artist(conn: Conn, fill: Fill) -> None:
    santana = catalog_artist(conn, "Santana", mbid(30))
    rob = catalog_artist(conn, "Rob Thomas", mbid(31))
    local = catalog_artist(conn, "Local Duo")  # no MBID
    featuring = log_name(conn, "SANTANA FT. ROB THOMAS", "manual_matched")
    link(conn, featuring, santana, origin="manual")
    line(conn, featuring, "Smooth", plays=9)
    line(conn, featuring, "SMOOTH", plays=1)
    line(conn, featuring, "Maria Maria (f/The Product G&B)", plays=4)
    line(conn, featuring, "Corazon Espinado", plays=6)
    cached(conn, f"recording-search:{mbid(30)}:corazon espinado:10")  # song matching asked it
    # Not C18:
    alone = log_name(conn, "CARLOS SANTANA", "manual_matched")  # no joiner
    link(conn, alone, santana, origin="manual")
    line(conn, alone, "Oye Como Va", plays=7)
    both = log_name(conn, "SANTANA & ROB THOMAS", "manual_matched")  # two links
    link(conn, both, santana, origin="manual")
    link(conn, both, rob, origin="manual")
    line(conn, both, "Why Don't You & I", plays=5)
    no_mbid = log_name(conn, "LOCAL & DUO", "manual_matched")  # its artist holds no MBID
    link(conn, no_mbid, local, origin="manual")
    line(conn, no_mbid, "Hometown", plays=5)
    automatic = log_name(conn, "SANTANA WITH ROB THOMAS", "auto_matched")  # not a manual link
    link(conn, automatic, santana)
    line(conn, automatic, "The Game of Love", plays=3)

    fill.run()

    assert fill.server.asked("recording") == [
        Asked("recording", title="smooth", mbid=mbid(30)),
        Asked("recording", title="maria maria", mbid=mbid(30)),
    ]
    assert searched(fill) == []
    assert f"recording-search:{mbid(30)}:smooth:10" in cache_keys(conn)


# --- confirmations -----------------------------------------------------------------------------


def test_confirmations_are_searched_oldest_first(conn: Conn, fill: Fill) -> None:
    boston = log_name(conn, "BOSTON", "auto_matched")  # a decided name is still searched
    kansas = log_name(conn, "KANSAS")
    styx = log_name(conn, "STYX", "manual_rejected")  # AUD-R029: never
    various = log_name(conn, "VARIOUS ARTISTS")  # the not-an-artist list: never
    confirmation(conn, kansas, "Dust in the Wind", T0 + timedelta(minutes=2))
    confirmation(conn, boston, "More Than a Feeling", T0)
    confirmation(conn, styx, "Renegade", T0 + timedelta(minutes=1))
    confirmation(conn, various, "Greatest Hits", T0 + timedelta(minutes=3))
    # Payoff fills this pair first in the same run, so its confirmation asks nothing.
    line(conn, kansas, "Carry On Wayward Son", plays=4)
    library_file(conn, "Carry On Wayward Son", mbids=[mbid(1)])
    confirmation(conn, kansas, "Carry On Wayward Son", T0 - timedelta(minutes=1))

    fill.run()

    assert searched(fill) == [
        Asked("name_title", ("KANSAS",), "Carry On Wayward Son"),  # payoff
        Asked("name_title", ("BOSTON",), "More Than a Feeling"),
        Asked("name_title", ("KANSAS",), "Dust in the Wind"),
    ]
