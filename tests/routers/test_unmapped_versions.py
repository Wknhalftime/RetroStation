"""The Unmapped versions page, the new-version marker and its counts (versions phase 2b PR 3, SPEC
S7, S8, D2, D5; design C62; AUD-R034).

``GET /library/versions?offset&limit`` lists every unmapped (work, version text) that reads as a
version, over all works, paged (50 by default) and sorted by plays, most first, then by work title
and text. Each item carries its work and artist, the work page's fields for the text, and the
marker; the page answers ``total`` and ``new_total``. Mapped and dismissed texts, title words and
texts that read as the original (question C) aren't listed.

S8, D2: a listed text is new when it has no state row, or when a copy of its version on the work
(a present file of the work, of the recording of that version) was indexed after its ``seen_at``:
a new file, or a re-tag, "may answer" it. A dismissed text never warns again. The marker is read,
never written: mapping or dismissing writes the state, seen now.

``POST /library/versions/dismiss`` takes exactly one of ``entries`` (Dismiss selected), ``all``
(Dismiss all on the page, every listed item on every page, new or seen) or ``artist_id`` (the
artist page's Dismiss all: the new versions of the group's works, D5 and question D); it answers
how many it dismissed.

D5: the artist list's ``new_version_count`` counts the new texts of every work of the group on its
primary, as ``work_count`` does; the artist page's count is the group's, and each of its works
carries its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from tests.routers import version_pages_seed as seed
from tests.routers.version_pages_seed import AFTER, BEFORE, T0

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.routers.version_pages_seed import Conn

pytestmark = pytest.mark.integration

PAGE = "/api/v1/library/versions"
DISMISS = "/api/v1/library/versions/dismiss"
ARTISTS = "/api/v1/library/artists"


def page(client: TestClient, **params: int) -> dict[str, Any]:
    response = client.get(PAGE, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def listed(client: TestClient) -> list[tuple[str, str]]:
    """(work_id, version_text) of every item, in order."""
    return [(i["work_id"], i["version_text"]) for i in page(client, limit=500)["items"]]


def played(conn: Conn, title: str, file_id: UUID, count: int, call_letters: str = "KISW") -> UUID:
    """A line titled *title* matched to *file_id*, with *count* plays."""
    line = seed.line(conn, title, file_id)
    times = [datetime(2001, 1, 1 + n, 8, tzinfo=UTC) for n in range(count)]
    seed.plays(conn, line, seed.station(conn, call_letters), *times)
    return line


def new_of(client: TestClient) -> dict[tuple[str, str], bool]:
    return {(i["work_id"], i["version_text"]): i["new"] for i in page(client, limit=500)["items"]}


# --- S7: the page -----------------------------------------------------------------------------


def test_lists_unmapped_versions(client: TestClient, db_conn: Conn) -> None:
    """S7: unmapped texts that read as versions, over every work, by plays, then work title and
    text; each with its work, artist, plays, stations, first seen and suggestion."""
    metallica = seed.artist(db_conn, "Metallica")
    one = seed.work(db_conn, "One", metallica)
    one_master = seed.file(db_conn, one)
    live_copy = seed.file(db_conn, one, version_type="live")
    played(db_conn, "One (Live)", one_master, 5)
    played(db_conn, "One (Lv)", one_master, 2, "WXRT")
    played(db_conn, "One (Demo)", one_master, 2)
    played(db_conn, "One (Part 1)", one_master, 9)  # title words
    played(db_conn, "One (Album Version)", one_master, 9)  # the original
    played(db_conn, "One (Acoustic)", one_master, 9)
    seed.state(db_conn, one, "acoustic", "dismissed")
    played(db_conn, "One (Unplugged)", one_master, 9)
    seed.entry(db_conn, one, "unplugged", live_copy)
    korn = seed.artist(db_conn, "Korn")
    freak = seed.work(db_conn, "Freak On A Leash", korn)
    played(db_conn, "Freak On A Leash (Unplugged)", seed.file(db_conn, freak), 2)
    lone = seed.work(db_conn, "Blind", korn)
    pending = seed.line(db_conn, "Blind (Live)", seed.file(db_conn, lone), status="pending")
    seed.plays(db_conn, pending, seed.station(db_conn, "KROQ"), datetime(2001, 1, 1, tzinfo=UTC))
    db_conn.commit()

    body = page(client)

    assert [(i["work_title"], i["version_text"], i["plays"]) for i in body["items"]] == [
        ("One", "live", 5),
        ("Freak On A Leash", "unplugged", 2),
        ("One", "demo", 2),
        ("One", "lv", 2),
    ]
    assert (body["total"], body["new_total"]) == (4, 4)
    first = body["items"][0]
    assert {k: first[k] for k in ("work_id", "artist_id", "artist_name", "version_type")} == {
        "work_id": one,
        "artist_id": metallica,
        "artist_name": "Metallica",
        "version_type": "live",
    }
    assert (first["stations"], first["first_seen"], first["first_station"]) == (
        ["KISW"],
        "2001-01-01",
        "KISW",
    )
    assert (first["suggestion"]["file_id"], first["suggestion"]["reason"]) == (
        str(live_copy),
        "only_copy",
    )
    assert body["items"][3]["stations"] == ["WXRT"]


def test_paged_at_50(client: TestClient, db_conn: Conn) -> None:
    """S7, DoR: 50 a page by default; ``offset`` turns it; ``limit`` is 1 to 500. Equal plays
    sort by work title."""
    works = [seed.work(db_conn, f"Song {n:02d}") for n in range(51)]
    for work in reversed(works):
        played(db_conn, "Song (Live)", seed.file(db_conn, work), 1)
    db_conn.commit()

    first = page(client)
    second = page(client, offset=50)
    small = page(client, offset=2, limit=2)

    assert (len(first["items"]), first["total"]) == (50, 51)
    assert [i["work_id"] for i in first["items"]] == works[:50]
    assert [i["work_id"] for i in second["items"]] == works[50:]
    assert [i["work_id"] for i in small["items"]] == works[2:4]
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert client.get(PAGE, params=bad).status_code == 422, bad


def test_empty(client: TestClient) -> None:
    assert page(client) == {"items": [], "total": 0, "new_total": 0}


def test_a_line_is_on_its_files_own_work(client: TestClient, db_conn: Conn) -> None:
    """Over all works too, a line's work is its file's own work, not the work of the file's
    recording (a split file keeps its recording); a file with no work of its own takes its
    recording's."""
    owner = seed.work(db_conn, "Orion")
    lender = seed.work(db_conn, "Orion (Instrumental)")
    played(db_conn, "Orion (Live)", seed.file(db_conn, owner, recording_work=lender), 2)
    played(db_conn, "Orion (Demo)", seed.file(db_conn, None, recording_work=lender), 1)
    db_conn.commit()

    assert [(i["work_id"], i["work_title"], i["version_text"]) for i in page(client)["items"]] == [
        (owner, "Orion", "live"),
        (lender, "Orion (Instrumental)", "demo"),
    ]


# --- S8: the marker ---------------------------------------------------------------------------


def test_new_and_may_answer(client: TestClient, db_conn: Conn) -> None:
    """S8, D2: new when there is no state row, or when a copy of that version on the work was
    indexed after ``seen_at``; never for a dismissed text, another version's file, a missing
    file, or a file of another work on this work's recording."""
    cases: dict[str, str] = {}

    def song(name: str) -> tuple[str, UUID]:
        work = seed.work(db_conn, name)
        cases[name] = work
        master = seed.file(db_conn, work)
        played(db_conn, f"{name} (Live)", master, 1)
        return work, master

    song("no state")
    work, _ = song("seen, no copy")
    seed.state(db_conn, work, "live", None, T0)
    work, _ = song("seen, copy indexed after")
    seed.state(db_conn, work, "live", None, T0)
    seed.file(db_conn, work, version_type="live", indexed_at=AFTER)
    work, _ = song("seen, copy indexed before")
    seed.state(db_conn, work, "live", None, T0)
    seed.file(db_conn, work, version_type="live", indexed_at=BEFORE)
    work, _ = song("seen, other version after")
    seed.state(db_conn, work, "live", None, T0)
    seed.file(db_conn, work, version_type="acoustic", indexed_at=AFTER)
    work, _ = song("seen, missing copy after")
    seed.state(db_conn, work, "live", None, T0)
    seed.file(db_conn, work, version_type="live", status="missing", indexed_at=AFTER)
    work, _ = song("seen, their copy after")
    seed.state(db_conn, work, "live", None, T0)
    seed.file(
        db_conn, seed.work(db_conn), version_type="live", recording_work=work, indexed_at=AFTER
    )
    work, _ = song("dismissed, copy after")
    seed.state(db_conn, work, "live", "dismissed", T0)
    seed.file(db_conn, work, version_type="live", indexed_at=AFTER)
    work, master = song("title words")
    played(db_conn, "title words (Part 1)", master, 1)
    db_conn.commit()

    marks = new_of(client)
    body = page(client)

    assert {name: marks.get((work, "live")) for name, work in cases.items()} == {
        "no state": True,
        "seen, no copy": False,
        "seen, copy indexed after": True,
        "seen, copy indexed before": False,
        "seen, other version after": False,
        "seen, missing copy after": False,
        "seen, their copy after": False,
        "dismissed, copy after": None,  # not listed
        "title words": True,
    }
    assert (cases["title words"], "part 1") not in marks
    assert (body["total"], body["new_total"]) == (8, 3)
    work_rows = client.get(f"/api/v1/library/works/{cases['dismissed, copy after']}/versions")
    assert [(v["state"], v["new"]) for v in work_rows.json()["versions"]] == [("dismissed", False)]
    titled = client.get(f"/api/v1/library/works/{cases['title words']}/versions").json()
    assert {v["version_text"]: v["new"] for v in titled["versions"]} == {
        "live": True,
        "part 1": False,
    }


def test_mapping_or_dismissing_clears_the_marker(client: TestClient, db_conn: Conn) -> None:
    """S8: the routes write the state, seen now, so neither text is new afterwards; a mapped or
    dismissed text leaves the page."""
    work = seed.work(db_conn, "Fuel")
    master = seed.file(db_conn, work)
    live = seed.file(db_conn, work, version_type="live", indexed_at=AFTER)
    played(db_conn, "Fuel (Live)", master, 3)
    played(db_conn, "Fuel (Demo)", master, 2)
    seed.state(db_conn, work, "live", None, T0)
    db_conn.commit()
    assert new_of(client) == {(work, "live"): True, (work, "demo"): True}

    mapped = client.post(
        f"/api/v1/library/works/{work}/version-entries",
        json={"version_texts": ["live"], "file_id": str(live)},
    )
    dismissed = client.post(
        f"/api/v1/library/works/{work}/versions/dismiss", json={"version_texts": ["demo"]}
    )

    assert (mapped.status_code, dismissed.status_code) == (204, 204)
    assert page(client) == {"items": [], "total": 0, "new_total": 0}
    work_rows = client.get(f"/api/v1/library/works/{work}/versions").json()["versions"]
    assert {v["version_text"]: v["new"] for v in work_rows} == {"live": False, "demo": False}


# --- S7: Dismiss selected and Dismiss all -----------------------------------------------------


def test_dismiss_selected(client: TestClient, db_conn: Conn) -> None:
    """Dismiss selected dismisses the named items, seen now; an unknown one is 422 and a mapped
    one 409, and either refusal writes nothing."""
    first = seed.work(db_conn, "Wherever I May Roam")
    first_master = seed.file(db_conn, first)
    played(db_conn, "Wherever I May Roam (Live)", first_master, 3)
    played(db_conn, "Wherever I May Roam (Demo)", first_master, 2)
    second = seed.work(db_conn, "The Unforgiven")
    second_master = seed.file(db_conn, second)
    played(db_conn, "The Unforgiven (Live)", second_master, 1)
    seed.entry(db_conn, second, "live", second_master)
    played(db_conn, "The Unforgiven (Acoustic)", second_master, 1)
    db_conn.commit()
    before = seed.now(db_conn)

    unknown = client.post(
        DISMISS,
        json={
            "entries": [
                {"work_id": first, "version_text": "live"},
                {"work_id": first, "version_text": "unplugged"},
            ]
        },
    )
    mapped = client.post(
        DISMISS,
        json={
            "entries": [
                {"work_id": first, "version_text": "live"},
                {"work_id": second, "version_text": "live"},
            ]
        },
    )
    assert (unknown.status_code, mapped.status_code) == (422, 409)
    assert seed.all_states(db_conn) == set()

    done = client.post(
        DISMISS,
        json={
            "entries": [
                {"work_id": first, "version_text": "live"},
                {"work_id": second, "version_text": "acoustic"},
            ]
        },
    )

    assert (done.status_code, done.json()) == (200, {"dismissed": 2})
    assert listed(client) == [(first, "demo")]
    states = seed.states_of(db_conn, first) | seed.states_of(db_conn, second)
    assert {t: kind for t, (kind, _) in states.items()} == {
        "live": "dismissed",
        "acoustic": "dismissed",
    }
    assert all(seen_at >= before for _, seen_at in states.values())


def test_dismiss_all(client: TestClient, db_conn: Conn) -> None:
    """C62: Dismiss all on the page dismisses every listed item, on every page, new or seen, and
    nothing else: mapped texts, title words and texts already dismissed keep their state, and
    every version entry stays (the lines keep the master or their file)."""
    works = [seed.work(db_conn, f"Track {n}") for n in range(3)]
    for work in works:
        played(db_conn, "Track (Live)", seed.file(db_conn, work), 1)
    kept = works[0]
    kept_file = seed.file(db_conn, kept, version_type="demo")
    played(db_conn, "Track (Demo)", kept_file, 1)
    seed.entry(db_conn, kept, "demo", kept_file)
    seed.state(db_conn, kept, "demo", None, T0)
    played(db_conn, "Track (Part 1)", kept_file, 1)
    played(db_conn, "Track (Acoustic)", kept_file, 1)
    seed.state(db_conn, kept, "acoustic", "dismissed", T0)
    played(db_conn, "Track (Unplugged)", kept_file, 1)
    seed.state(db_conn, kept, "unplugged", None, T0)  # seen, not new: still listed
    db_conn.commit()
    assert page(client, limit=2)["total"] == 4
    assert page(client)["new_total"] == 3

    response = client.post(DISMISS, json={"all": True})

    assert (response.status_code, response.json()) == (200, {"dismissed": 4})
    assert page(client) == {"items": [], "total": 0, "new_total": 0}
    assert seed.all_states(db_conn) == {
        *((work, "live", "dismissed") for work in works),
        (kept, "demo", None),
        (kept, "acoustic", "dismissed"),
        (kept, "unplugged", "dismissed"),
    }
    assert seed.states_of(db_conn, kept)["acoustic"] == ("dismissed", T0)
    assert seed.entries_of(db_conn, kept) == {"demo": kept_file}


def test_dismiss_all_on_the_artist_page(client: TestClient, db_conn: Conn) -> None:
    """D5: the artist page's Dismiss all covers the new versions of every work of the artist's
    group, the primary's and its merged members', and no other artist's (question D: the "N new
    versions" the page counts and confirms; a seen one stays listed). A member's id finds its
    group. An unknown artist is 404, and a body naming none or two of the three kinds is 422."""
    prince = seed.artist(db_conn, "Prince")
    revolution = seed.artist(db_conn, "The Revolution")
    seed.merge(db_conn, revolution, prince)
    purple = seed.work(db_conn, "Purple Rain", prince)
    purple_master = seed.file(db_conn, purple)
    played(db_conn, "Purple Rain (Live)", purple_master, 2)
    played(db_conn, "Purple Rain (Unplugged)", purple_master, 3)
    seed.state(db_conn, purple, "unplugged", None, T0)  # seen, not new
    computer = seed.work(db_conn, "Computer Blue", revolution)
    played(db_conn, "Computer Blue (Demo)", seed.file(db_conn, computer), 1)
    other = seed.work(db_conn, "When Doves Cry", seed.artist(db_conn, "Not Prince"))
    played(db_conn, "When Doves Cry (Live)", seed.file(db_conn, other), 1)
    member_only = seed.artist(db_conn, "Wendy & Lisa")
    seed.merge(db_conn, member_only, prince)
    db_conn.commit()

    for bad in ({}, {"all": True, "artist_id": prince}, {"artist_id": prince, "entries": []}):
        assert client.post(DISMISS, json=bad).status_code == 422, bad
    assert client.post(DISMISS, json={"artist_id": "no-such-artist"}).status_code == 404

    response = client.post(DISMISS, json={"artist_id": prince})

    assert (response.status_code, response.json()) == (200, {"dismissed": 2})
    assert listed(client) == [(purple, "unplugged"), (other, "live")]
    assert seed.states_of(db_conn, purple)["unplugged"] == (None, T0)

    played(db_conn, "Purple Rain (Acoustic)", purple_master, 1)
    db_conn.commit()
    by_member = client.post(DISMISS, json={"artist_id": member_only})
    assert (by_member.status_code, by_member.json()) == (200, {"dismissed": 1})
    assert listed(client) == [(purple, "unplugged"), (other, "live")]


# --- D5: counts on the group primary ----------------------------------------------------------


def test_new_counts_on_the_group_primary(client: TestClient, db_conn: Conn) -> None:
    """D5: the artist list counts the group's new texts on its primary (a merged member isn't
    listed); the artist page answers the group's count and each work's own. Only new versions
    count: not a seen one, title words, a dismissed or a mapped text, though a copy was indexed
    after each one's state. The fields are optional: an artist with none answers 0."""
    prince = seed.artist(db_conn, "Prince")
    revolution = seed.artist(db_conn, "The Revolution")
    seed.merge(db_conn, revolution, prince)
    purple = seed.work(db_conn, "Purple Rain", prince)
    purple_master = seed.file(db_conn, purple)
    played(db_conn, "Purple Rain (Live)", purple_master, 2)
    played(db_conn, "Purple Rain (Acoustic)", purple_master, 1)
    seed.state(db_conn, purple, "acoustic", None, T0)
    played(db_conn, "Purple Rain (Part 1)", purple_master, 1)
    seed.file(db_conn, purple, version_type="demo", indexed_at=AFTER)
    played(db_conn, "Purple Rain (Demo)", purple_master, 1)
    seed.state(db_conn, purple, "demo", "dismissed", T0)
    remix = seed.file(db_conn, purple, version_type="remix", indexed_at=AFTER)
    played(db_conn, "Purple Rain (Remix)", purple_master, 1)
    seed.entry(db_conn, purple, "remix", remix)
    seed.state(db_conn, purple, "remix", None, T0)
    computer = seed.work(db_conn, "Computer Blue", revolution)
    computer_master = seed.file(db_conn, computer)
    played(db_conn, "Computer Blue (Demo)", computer_master, 1)
    played(db_conn, "Computer Blue (Live)", computer_master, 1)
    quiet = seed.artist(db_conn, "Quiet Artist")
    seed.file(db_conn, seed.work(db_conn, "Silence", quiet))
    db_conn.commit()

    items = client.get(ARTISTS, params={"limit": 50}).json()["items"]
    counts = {i["id"]: i["new_version_count"] for i in items}
    detail = client.get(f"{ARTISTS}/{prince}").json()

    assert counts == {prince: 3, quiet: 0}
    assert detail["new_version_count"] == 3
    assert {w["id"]: w["new_version_count"] for w in detail["works"]} == {
        purple: 1,
        computer: 2,
    }
