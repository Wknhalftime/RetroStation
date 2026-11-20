"""What the work page shows, from what the reader reads (artist model phase 3b PR 5a; SPEC S8,
D41; design C61, C71, C72).

``work_page`` is pure. From the work's performances, its master and its lines (one row per
station and line, as the air-play view resolves it), it gives:

- the performances, the master's first and marked default (C71), then by credit;
- each performance's credit as one text, and the lines that play it through a stored pick;
- under each copy, the lines it holds through a merge pin, each with what it plays next: the
  master when it plays its pin now, else what it plays now, since a step read before the pin
  already wins (C61, C72);
- the work's credited artists, the default's first (D41 g);
- its acts, when its performances credit two or more: the default act's lines are the ones on
  the default path, which an override or a version entry reaches, by station format; another
  act's are the lines that store its performance (D41 f).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.work_page import (
    Act,
    CreditName,
    MasterCopy,
    PerformanceFacts,
    PinnedLine,
    PlaySource,
    WorkLine,
    credit_text,
    work_page,
)

LABELLE = CreditName("a-labelle", "LaBelle")
AGUILERA = CreditName("a-aguilera", "Christina Aguilera")
KIM = CreditName("a-kim", "Lil' Kim")


def ids(n: int) -> list[UUID]:
    return sorted(uuid4() for _ in range(n))


def performance(
    copies: list[UUID],
    *credit: CreditName,
    preferred: UUID | None = None,
    version: str = "original",
    performance_id: UUID | None = None,
) -> PerformanceFacts:
    return PerformanceFacts(
        id=performance_id or uuid4(),
        credit=credit,
        version_type=version,
        preferred_copy_id=preferred,
        copy_ids=tuple(copies),
    )


def line(
    identity_id: UUID,
    file_id: UUID | None,
    source: PlaySource | None,
    *,
    station: str = "KISW",
    station_format: str | None = "AOR",
    plays: int = 1,
    pinned: UUID | None = None,
    title: str = "Lady Marmalade",
) -> WorkLine:
    return WorkLine(
        identity_id=identity_id,
        title=title,
        artist="LABELLE",
        station_id=UUID(int=sum(map(ord, station))),
        station=station,
        station_format=station_format,
        plays=plays,
        file_id=file_id,
        path=None if file_id is None else f"D:/music/{file_id}.flac",
        source=source,
        pinned_file_id=pinned,
    )


# --- order, default, credit ---------------------------------------------------------------------


def test_the_masters_performance_is_the_default_and_comes_first() -> None:
    a, b, c = ids(3)
    zed = performance([a], CreditName("a-z", "Zed"))
    labelle = performance([b], LABELLE)
    aguilera = performance([c], AGUILERA)

    page = work_page([zed, labelle, aguilera], MasterCopy(b, "b.flac"), [])

    assert [(p.id, p.default) for p in page.performances] == [
        (labelle.id, True),
        (aguilera.id, False),
        (zed.id, False),
    ]


def test_with_no_master_no_performance_is_the_default() -> None:
    a, b = ids(2)
    labelle = performance([a], LABELLE)
    aguilera = performance([b], AGUILERA)

    page = work_page([labelle, aguilera], None, [])

    assert [(p.id, p.default) for p in page.performances] == [
        (aguilera.id, False),
        (labelle.id, False),
    ]


def test_one_credit_lists_the_original_first_then_versions_then_by_id() -> None:
    live = performance([uuid4()], LABELLE, version="live", performance_id=UUID(int=1))
    original_b = performance([uuid4()], LABELLE, performance_id=UUID(int=3))
    original_a = performance([uuid4()], LABELLE, performance_id=UUID(int=2))
    acoustic = performance([uuid4()], LABELLE, version="acoustic", performance_id=UUID(int=4))

    page = work_page([live, original_b, acoustic, original_a], None, [])

    assert [p.id for p in page.performances] == [
        original_a.id,
        original_b.id,
        acoustic.id,
        live.id,
    ]


def test_credit_text_joins_by_each_credits_own_phrase_else_a_comma() -> None:
    parts = (
        CreditName("a-1", "Christina Aguilera", " & "),
        CreditName("a-2", "Lil' Kim"),
        CreditName("a-3", "Mýa"),
        CreditName(None, "Pink"),
    )

    assert credit_text(parts) == "Christina Aguilera & Lil' Kim, Mýa, Pink"
    assert credit_text(()) == ""


def test_each_performance_carries_its_credit_text() -> None:
    copy = uuid4()
    duet = performance(
        [copy], CreditName("a-1", "Bon Jovi", " feat. "), CreditName("a-2", "Nettles")
    )

    (shown,) = work_page([duet], None, []).performances

    assert shown.credit_text == "Bon Jovi feat. Nettles"
    assert shown.credit == duet.credit


def test_the_artists_are_the_defaults_first_then_the_rest_each_once() -> None:
    a, b, c = ids(3)
    covers = performance([a], AGUILERA, KIM, LABELLE)
    original = performance([b], LABELLE)
    nameless = performance([c], CreditName(None, "f00d-mbid"))

    page = work_page([covers, original, nameless], MasterCopy(b, "b.flac"), [])

    assert page.artists == (LABELLE, AGUILERA, KIM, CreditName(None, "f00d-mbid"))


# --- lines that play a stored performance -------------------------------------------------------


def test_a_performance_counts_each_line_that_plays_it_once() -> None:
    master, cover = ids(2)
    one, two, three = ids(3)
    labelle = performance([master], LABELLE)
    aguilera = performance([cover], AGUILERA, preferred=cover)
    lines = [
        line(one, cover, PlaySource.PERFORMANCE, station="KISW"),
        line(one, cover, PlaySource.PERFORMANCE, station="KZOK"),
        line(two, cover, PlaySource.PERFORMANCE),
        line(three, master, PlaySource.MASTER),
    ]

    page = work_page([labelle, aguilera], MasterCopy(master, "m.flac"), lines)

    assert {p.id: p.lines for p in page.performances} == {labelle.id: 0, aguilera.id: 2}


# --- pins ---------------------------------------------------------------------------------------


def test_a_line_that_plays_its_pin_plays_the_master_next() -> None:
    master, merged = ids(2)
    held = uuid4()
    labelle = performance([master, merged], LABELLE)
    lines = [line(held, merged, PlaySource.PIN, plays=3, pinned=merged)]

    page = work_page([labelle], MasterCopy(master, "D:/music/master.flac"), lines)

    copies = {c.file_id: c.pins for c in page.performances[0].copies}
    assert copies[master] == ()
    assert copies[merged] == (
        PinnedLine(
            identity_id=held,
            station_id=lines[0].station_id,
            station="KISW",
            title="Lady Marmalade",
            artist="LABELLE",
            plays=3,
            next_file_id=master,
            next_path="D:/music/master.flac",
        ),
    )


def test_a_pinned_line_a_step_before_the_pin_plays_keeps_playing_it_next() -> None:
    """An override, a version entry or a stored performance is read before the pin (C61);
    a pin on the master's own copy reads as the master. Release changes neither."""
    master, merged, edit = ids(3)
    held = uuid4()
    labelle = performance([master, merged, edit], LABELLE)
    lines = [
        line(held, edit, PlaySource.OVERRIDE, station="KZOK", pinned=merged),
        line(held, merged, PlaySource.PIN, station="KISW", pinned=merged),
        line(held, master, PlaySource.MASTER, station="KJR", pinned=master),
    ]

    page = work_page([labelle], MasterCopy(master, "m.flac"), lines)

    copies = {c.file_id: c.pins for c in page.performances[0].copies}
    assert {(p.station, p.next_file_id) for p in copies[merged]} == {
        ("KISW", master),
        ("KZOK", edit),
    }
    assert [(p.station, p.next_file_id) for p in copies[master]] == [("KJR", master)]


def test_a_pinned_line_with_no_master_plays_nothing_next() -> None:
    merged = uuid4()
    held = uuid4()
    labelle = performance([merged], LABELLE)

    page = work_page([labelle], None, [line(held, merged, PlaySource.PIN, pinned=merged)])

    (pinned,) = page.performances[0].copies[0].pins
    assert (pinned.next_file_id, pinned.next_path) == (None, None)


def test_pins_are_listed_by_title_then_line_then_station() -> None:
    merged = uuid4()
    first, second = ids(2)
    labelle = performance([merged], LABELLE)
    lines = [
        line(second, merged, PlaySource.PIN, station="KZOK", pinned=merged, title="b"),
        line(first, merged, PlaySource.PIN, station="KZOK", pinned=merged, title="a"),
        line(first, merged, PlaySource.PIN, station="KISW", pinned=merged, title="a"),
    ]

    page = work_page([labelle], None, lines)

    assert [(p.identity_id, p.station) for p in page.performances[0].copies[0].pins] == [
        (first, "KISW"),
        (first, "KZOK"),
        (second, "KZOK"),
    ]


# --- acts ---------------------------------------------------------------------------------------


def test_two_acts_list_the_lines_each_reaches() -> None:
    """LaBelle is the default: the lines on the default path are hers, and an override or a
    version entry reaches them, by format. The Aguilera lines store her performance, which the
    view plays first, so nothing set on the work reaches them."""
    master, cover = ids(2)
    one, two, three, four = ids(4)
    labelle = performance([master], LABELLE)
    aguilera = performance([cover], AGUILERA, KIM, preferred=cover)
    lines = [
        line(one, master, PlaySource.MASTER, station="KISW", station_format="AOR"),
        line(one, master, PlaySource.MASTER, station="KZOK", station_format="Classic Hits"),
        line(two, master, PlaySource.OVERRIDE, station="KJR", station_format="AOR"),
        line(three, master, PlaySource.VERSION, station="KISW", station_format="AOR"),
        line(four, cover, PlaySource.PERFORMANCE, station="KISW", station_format="AOR"),
    ]

    page = work_page([aguilera, labelle], MasterCopy(master, "m.flac"), lines)

    assert page.acts == (
        Act("LaBelle", default=True, lines=3, reached={"AOR": 3, "Classic Hits": 1}),
        Act("Christina Aguilera, Lil' Kim", default=False, lines=1, reached={}),
    )


def test_one_credit_in_several_performances_is_one_act_and_lists_none() -> None:
    a, b = ids(2)
    studio = performance([a], LABELLE)
    live = performance([b], LABELLE, version="live")

    assert work_page([studio, live], MasterCopy(a, "a.flac"), []).acts == ()


def test_an_uncredited_performance_is_no_act() -> None:
    a, b = ids(2)
    credited = performance([a], LABELLE)
    uncredited = performance([b])

    assert work_page([credited, uncredited], MasterCopy(a, "a.flac"), []).acts == ()


def test_the_same_artists_in_another_order_are_one_act() -> None:
    a, b, c = ids(3)
    one = performance([a], AGUILERA, KIM)
    other = performance([b], KIM, AGUILERA)
    labelle = performance([c], LABELLE)

    acts = work_page([one, other, labelle], MasterCopy(c, "c.flac"), []).acts

    assert [act.credit for act in acts] == ["LaBelle", "Christina Aguilera, Lil' Kim"]


def test_with_no_default_act_the_default_path_counts_for_no_act() -> None:
    """No master, so nothing is the default: only stored lines name an act."""
    a, b = ids(2)
    stored, other = ids(2)
    labelle = performance([a], LABELLE, preferred=a)
    aguilera = performance([b], AGUILERA, preferred=b)
    lines = [
        line(stored, b, PlaySource.PERFORMANCE),
        line(other, a, PlaySource.OVERRIDE),
    ]

    acts = work_page([labelle, aguilera], None, lines).acts

    assert acts == (
        Act("Christina Aguilera", default=False, lines=1, reached={}),
        Act("LaBelle", default=False, lines=0, reached={}),
    )


def test_a_line_that_plays_nothing_counts_for_no_act() -> None:
    master, cover = ids(2)
    silent = uuid4()
    labelle = performance([master], LABELLE)
    aguilera = performance([cover], AGUILERA)

    acts = work_page(
        [labelle, aguilera], MasterCopy(master, "m.flac"), [line(silent, None, None)]
    ).acts

    assert [act.lines for act in acts] == [0, 0]
